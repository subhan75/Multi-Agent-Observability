"""Prometheus metrics shared by every service.

Each service keeps running tallies in memory and serves them as text at GET /metrics.
Prometheus visits that page every few seconds ("scraping") and stores the numbers over time.

Note what is NOT a label here: no service name (Prometheus attaches `job`/`instance` itself when
it scrapes), no request id, no user text. Every distinct label combination becomes its own
time series held in memory forever, so labels must have a small, fixed set of values.
"""
import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

# --- HTTP metrics (the RED method: Rate, Errors, Duration) -----------------------------------
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"], buckets=HTTP_BUCKETS
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_flight", "HTTP requests currently being handled")

# --- AI / agent metrics -----------------------------------------------------------------------
LLM_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120)

LLM_CALLS = Counter("llm_calls_total", "Calls to the LLM", ["model", "caller", "outcome"])
LLM_DURATION = Histogram(
    "llm_call_duration_seconds", "LLM call latency", ["model", "caller"], buckets=LLM_BUCKETS
)
NODE_RUNS = Counter("agent_node_runs_total", "Agent graph node executions", ["node", "outcome"])
NODE_DURATION = Histogram(
    "agent_node_duration_seconds", "Agent graph node latency", ["node"], buckets=LLM_BUCKETS
)
SUPERVISOR_DECISIONS = Counter(
    "supervisor_decisions_total", "Which step the supervisor chose", ["choice", "guardrail_override"]
)

# Paths we do not measure: Prometheus scraping itself, our own event plumbing, and the
# long-lived SSE streams (their "duration" is meaningless and they would pin the in-flight gauge).
_SKIPPED_PREFIXES = ("/metrics", "/internal/events", "/events/")


def _route_template(request: Request) -> str:
    """'/orders/{order_id}', not '/orders/42': keeps the number of label values bounded."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


def install_metrics(app) -> None:
    """Add the /metrics endpoint and a middleware that records the HTTP metrics."""

    async def metrics_endpoint(request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)

    @app.middleware("http")
    async def _record_http_metrics(request: Request, call_next):
        if request.url.path.startswith(_SKIPPED_PREFIXES):
            return await call_next(request)
        HTTP_IN_FLIGHT.inc()
        started = time.perf_counter()
        status = 500  # if call_next raises, the request really did fail with a 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = _route_template(request)  # only known after the router has run
            HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
            HTTP_DURATION.labels(request.method, route).observe(time.perf_counter() - started)
            HTTP_IN_FLIGHT.dec()


# --- Helpers for the agent code ----------------------------------------------------------------
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
        LLM_CALLS.labels(model, caller, outcome).inc()
        LLM_DURATION.labels(model, caller).observe(time.perf_counter() - started)


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
                NODE_RUNS.labels(node, outcome).inc()
                NODE_DURATION.labels(node).observe(time.perf_counter() - started)

        return wrapper

    return decorator
