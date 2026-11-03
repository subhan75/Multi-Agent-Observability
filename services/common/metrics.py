"""Metrics, now built on OpenTelemetry (Phase 3; in Phase 1 this used the prometheus-client library).

The idea is unchanged: count and time things. What changed is the PLUMBING:

  Phase 1:  our code -> prometheus_client -> a /metrics page on EACH service <- Prometheus scrapes 4 services
  Phase 3:  our code -> OpenTelemetry     -> pushed every 5 s to the Collector -> its ONE /metrics page
                                                                                  <- Prometheus scrapes 1 place

We deliberately keep the SAME metric names, so every PromQL query from Phase 1 still works.
OpenTelemetry names are translated by the Collector on the way to Prometheus:
    counter   "http_requests"          -> http_requests_total
    histogram "http_request_duration"  -> http_request_duration_seconds_bucket / _sum / _count   (unit "s")
    up-down   "http_requests_in_flight"-> http_requests_in_flight

Labels are called "attributes" in OpenTelemetry. The same cardinality rules apply: no ids, no free text.
The service name is NOT an attribute of ours: it comes from the Resource set up in telemetry.py, and the
Collector turns it into the Prometheus label `job`.

EXEMPLARS: when a measurement is recorded while a trace span is active, OpenTelemetry automatically
attaches that span's trace id to the measurement. Prometheus keeps it, and Grafana can show it as a
clickable dot on a graph that opens the exact trace behind a slow request. No extra code needed here.
"""
import functools
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import metrics
from starlette.requests import Request

from services.common.pricing import cost_usd

HTTP_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120]
LLM_BUCKETS = [0.5, 1, 2, 5, 10, 20, 30, 60, 120]

# A "meter" is the factory for instruments. If telemetry.py has not run yet when this module is
# imported, OpenTelemetry hands out placeholder instruments that start working once it has.
_meter = metrics.get_meter("shopsense")

# --- HTTP metrics (the RED method: Rate, Errors, Duration) -----------------------------------
HTTP_REQUESTS = _meter.create_counter(
    "http_requests", unit="{request}", description="HTTP requests handled")
HTTP_DURATION = _meter.create_histogram(
    "http_request_duration", unit="s", description="HTTP request latency",
    explicit_bucket_boundaries_advisory=HTTP_BUCKETS)
HTTP_IN_FLIGHT = _meter.create_up_down_counter(
    "http_requests_in_flight", unit="{request}", description="HTTP requests currently being handled")

# --- AI / agent metrics -----------------------------------------------------------------------
LLM_CALLS = _meter.create_counter(
    "llm_calls", unit="{call}", description="Calls to the LLM")
LLM_DURATION = _meter.create_histogram(
    "llm_call_duration", unit="s", description="LLM call latency",
    explicit_bucket_boundaries_advisory=LLM_BUCKETS)
NODE_RUNS = _meter.create_counter(
    "agent_node_runs", unit="{run}", description="Agent graph node executions")
NODE_DURATION = _meter.create_histogram(
    "agent_node_duration", unit="s", description="Agent graph node latency",
    explicit_bucket_boundaries_advisory=LLM_BUCKETS)
SUPERVISOR_DECISIONS = _meter.create_counter(
    "supervisor_decisions", unit="{decision}", description="Which step the supervisor chose")
LLM_TOKENS = _meter.create_counter(
    "llm_tokens", unit="{token}", description="Tokens processed by the LLM (direction = input or output)")
LLM_COST = _meter.create_counter(
    "llm_cost_usd", unit="{usd}", description="Estimated LLM spend in US dollars (see services/common/model_prices.json)")
USER_FEEDBACK = _meter.create_counter(
    "user_feedback", unit="{vote}", description="Thumbs up / down given by users on answers")
TOOL_CALLS = _meter.create_counter(
    "agent_tool_calls", unit="{call}", description="Tool calls made by agents")

# Paths we do not measure: our own event plumbing and the long-lived SSE streams (their "duration"
# is meaningless and they would pin the in-flight gauge).
_SKIPPED_PREFIXES = ("/internal/events", "/events/")


def _route_template(request: Request) -> str:
    """'/orders/{order_id}', not '/orders/42': keeps the number of attribute values bounded."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


def install_metrics(app) -> None:
    """Add a middleware that records the HTTP metrics for every request.

    (There is no /metrics endpoint any more: the data is pushed to the Collector instead.)
    """

    @app.middleware("http")
    async def _record_http_metrics(request: Request, call_next):
        if request.url.path.startswith(_SKIPPED_PREFIXES):
            return await call_next(request)
        HTTP_IN_FLIGHT.add(1)
        started = time.perf_counter()
        status = 500  # if call_next raises, the request really did fail with a 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = _route_template(request)  # only known after the router has run
            HTTP_REQUESTS.add(1, {"method": request.method, "route": route, "status": str(status)})
            HTTP_DURATION.record(time.perf_counter() - started, {"method": request.method, "route": route})
            HTTP_IN_FLIGHT.add(-1)


# --- Helpers for the agent code ----------------------------------------------------------------
def record_llm_call(model: str, caller: str, seconds: float, outcome: str) -> None:
    """Count one finished model call and record how long it took. outcome is "ok" or "error"."""
    LLM_CALLS.add(1, {"model": model, "caller": caller, "outcome": outcome})
    LLM_DURATION.record(seconds, {"model": model, "caller": caller})


@contextmanager
def track_llm(model: str, caller: str) -> Iterator[None]:
    """Count and time one LLM call; records outcome=error if the body raises."""
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        record_llm_call(model, caller, time.perf_counter() - started, outcome)


def track_node(node: str) -> Callable:
    """Decorator: count and time every run of an agent graph node."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)  # keep the original function's name/docstring
        def wrapper(*args, **kwargs):
            started = time.perf_counter()
            outcome = "ok"
            try:
                return func(*args, **kwargs)
            except Exception:
                outcome = "error"
                raise
            finally:
                NODE_RUNS.add(1, {"node": node, "outcome": outcome})
                NODE_DURATION.record(time.perf_counter() - started, {"node": node})

        return wrapper

    return decorator


# A running total for the question being answered. The agents service puts a dict here before running the
# graph; every model call adds to it, so the answer can report "this question cost N tokens / $X".
# (A ContextVar holds a dict BY REFERENCE, so worker threads that copy the context still add to the same dict.)
request_usage_var: ContextVar[dict | None] = ContextVar("request_usage", default=None)
_usage_lock = threading.Lock()


def new_usage_tally() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "llm_calls": 0}


def record_llm_usage(model: str, caller: str, input_tokens: int, output_tokens: int) -> float:
    """Count the tokens of one model call, estimate its cost, add both to the current question's tally.

    Returns the estimated cost in USD. Thinking tokens are billed (and reported) as output tokens.
    """
    cost = cost_usd(model, input_tokens, output_tokens)
    LLM_TOKENS.add(input_tokens, {"model": model, "caller": caller, "direction": "input"})
    LLM_TOKENS.add(output_tokens, {"model": model, "caller": caller, "direction": "output"})
    LLM_COST.add(cost, {"model": model, "caller": caller})
    tally = request_usage_var.get()
    if tally is not None:
        with _usage_lock:
            tally["input_tokens"] += input_tokens
            tally["output_tokens"] += output_tokens
            tally["cost_usd"] += cost
            tally["llm_calls"] += 1
    return cost


def record_feedback(score: int) -> None:
    """Count one piece of user feedback: 1 = thumbs up, 0 = thumbs down."""
    USER_FEEDBACK.add(1, {"score": "up" if score == 1 else "down"})


def record_tool_call(tool: str, outcome: str) -> None:
    """Count one tool call. outcome is "ok", "not_found" (a normal answer: nothing matched) or "error"."""
    TOOL_CALLS.add(1, {"tool": tool, "outcome": outcome})


def record_supervisor_decision(choice: str, overridden: bool) -> None:
    """Count one supervisor decision, noting whether the code guardrail had to override the LLM."""
    SUPERVISOR_DECISIONS.add(1, {"choice": choice, "guardrail_override": str(overridden).lower()})
