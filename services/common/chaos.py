"""Injected problems, for practising how to find what is wrong.

Observability only proves its worth when something breaks. These three switchable problems let a learner break
the system on purpose, then find the cause using traces, metrics and logs:

  slow_llm         every (mock) model call takes 1.5-2.5 s longer  -> a long waterfall, a high LLM latency panel
  supervisor_loop  the (mock) supervisor keeps re-picking a worker that already ran; the code guardrail
                   overrides it                                    -> a skipped step, guardrail override metrics
  inventory_down   the inventory service answers 503              -> orders degrade quietly: an error span and a
                   warning log, but the customer still gets an answer

It is OFF unless the environment variable CHAOS_ENABLED is 1/true (docker-compose.yml turns it on for the
learner stack). When it is off the `X-Chaos` header is ignored everywhere, so nothing changes for anyone else.

How it travels: the browser's choice -> the gateway -> an `X-Chaos` HTTP header -> every service that handles
the request puts it in a ContextVar (a variable that is separate per request) and passes it on to the next hop,
exactly like the request id (see events.current_headers).
"""
import os
from contextvars import ContextVar

HEADER = "X-Chaos"

SCENARIOS = {
    "slow_llm": "Slow AI model",
    "supervisor_loop": "Supervisor loops",
    "inventory_down": "Inventory down",
}

chaos_var: ContextVar[str | None] = ContextVar("chaos", default=None)


def enabled() -> bool:
    return os.getenv("CHAOS_ENABLED", "").strip().lower() in ("1", "true", "yes")


def parse(value: str | None) -> str | None:
    """The scenario name if injection is enabled and the name is known, otherwise None (ignore it)."""
    return value if enabled() and value in SCENARIOS else None


def active(name: str) -> bool:
    return chaos_var.get() == name


def install_chaos_middleware(app) -> None:
    """Adopt an incoming `X-Chaos` header for the duration of the request (only when injection is enabled)."""

    @app.middleware("http")
    async def _adopt_chaos(request, call_next):
        scenario = parse(request.headers.get(HEADER))
        if scenario is None:
            return await call_next(request)
        token = chaos_var.set(scenario)
        try:
            return await call_next(request)
        finally:
            chaos_var.reset(token)
