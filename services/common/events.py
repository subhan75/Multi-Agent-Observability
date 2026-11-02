"""Request X-ray events: how every service reports "what I'm doing" to the gateway.

Every event carries a request_id so the UI can group them. The id travels between services
in the `X-Request-ID` HTTP header and inside a service in a ContextVar. This is a hand-rolled
version of what OpenTelemetry's trace context does (Phase 2).
"""
import json
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx
from opentelemetry.instrumentation.utils import suppress_instrumentation

from services.common.config import service_url

GATEWAY_URL = service_url("GATEWAY_URL", 8000)
REQUEST_ID_HEADER = "X-Request-ID"

# A ContextVar is like a global that is separate for each request/task/thread.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_headers() -> dict[str, str]:
    """Headers to attach to an outgoing HTTP call so the next service joins the same request."""
    rid = request_id_var.get()
    return {REQUEST_ID_HEADER: rid} if rid else {}


def _jsonable(value: Any, limit: int = 4000) -> Any:
    """Make any payload JSON-safe and bounded in size."""
    text = json.dumps(value, default=str)
    if len(text) > limit:
        return {"truncated": text[:limit] + "…"}
    return json.loads(text)


def make_event(
    request_id: str,
    component: str,
    kind: str,
    payload: dict | None = None,
    *,
    caller: str | None = None,
    duration_ms: float | None = None,
    span_id: str | None = None,
) -> dict:
    return {
        "request_id": request_id,
        "ts": time.time(),
        "component": component,
        "kind": kind,
        "caller": caller,
        "span_id": span_id,
        "duration_ms": None if duration_ms is None else round(duration_ms, 1),
        "payload": _jsonable(payload or {}),
    }


# --- Non-blocking delivery: callers put events on a queue, one background thread posts them.
_outbox: queue.Queue[dict] = queue.Queue(maxsize=2000)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _drain_outbox() -> None:
    with httpx.Client(timeout=2) as client:
        while True:
            event = _outbox.get()
            try:
                with suppress_instrumentation():  # do not trace the tracing plumbing itself
                    client.post(f"{GATEWAY_URL}/internal/events", json=event)
            except httpx.HTTPError:
                pass  # observability must never break the app


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = threading.Thread(target=_drain_outbox, daemon=True, name="event-sender")
            _worker.start()


def emit_event(
    component: str,
    kind: str,
    payload: dict | None = None,
    *,
    caller: str | None = None,
    duration_ms: float | None = None,
    span_id: str | None = None,
) -> None:
    """Report one event for the current request. Silently does nothing outside a request."""
    request_id = request_id_var.get()
    if request_id is None:
        return
    _ensure_worker()
    event = make_event(request_id, component, kind, payload, caller=caller, duration_ms=duration_ms, span_id=span_id)
    try:
        _outbox.put_nowait(event)
    except queue.Full:
        pass


@contextmanager
def step(component: str, caller: str | None, input_payload: dict | None = None) -> Iterator[dict]:
    """Wrap a unit of work: emits step_start, then step_end (with duration) or step_error.

    The body fills the yielded dict with its output:  `with step(...) as out: out["answer"] = ...`
    """
    span_id = uuid.uuid4().hex[:8]
    emit_event(component, "step_start", input_payload, caller=caller, span_id=span_id)
    output: dict = {}
    started = time.perf_counter()
    try:
        yield output
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        emit_event(component, "step_error", {"error": repr(exc)}, duration_ms=elapsed, span_id=span_id)
        raise
    elapsed = (time.perf_counter() - started) * 1000
    emit_event(component, "step_end", output, duration_ms=elapsed, span_id=span_id)


def install_event_middleware(app, component: str | None = None, caller: str | None = None) -> None:
    """Add middleware that adopts the incoming X-Request-ID.

    If `component` is given, also reports each request to this service as a step.
    """

    @app.middleware("http")
    async def _adopt_request_id(request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER)
        if not rid or request.url.path == "/health":
            return await call_next(request)
        token = request_id_var.set(rid)
        try:
            if component is None:
                return await call_next(request)
            with step(component, caller, {"summary": f"{request.method} {request.url.path}"}) as out:
                response = await call_next(request)
                out["status"] = response.status_code
                out["summary"] = f"{request.method} {request.url.path} → {response.status_code}"
            return response
        finally:
            request_id_var.reset(token)
