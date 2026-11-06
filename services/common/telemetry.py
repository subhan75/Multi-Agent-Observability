"""OpenTelemetry traces (Phase 2), metrics (Phase 3) and logs (Phase 4), shared by every service.

What this gives us:
  * AUTOMATIC spans for every incoming web request (FastAPI), every outgoing HTTP call (httpx,
    which also covers the calls the Gemini SDK makes) and every SQLite query.
  * The W3C `traceparent` header, added to outgoing calls and read from incoming ones, so one
    request keeps the SAME trace id across the gateway, agents, orders and inventory services.
    (This is the professional version of our hand-made X-Request-ID header.)
  * Helpers for MANUAL spans around the things OTel cannot guess: agent steps, LLM calls, tools.

Spans are batched and sent to the OpenTelemetry Collector, which forwards them to Tempo.
Metrics (counters, histograms) are sent to the same Collector every 5 seconds; it exposes them on
a page that Prometheus scrapes. Python log records are sent the same way and the Collector forwards
them to Loki; every log written inside a request automatically carries that request's trace id and
span id. If the Collector is down, services keep working; data is dropped.
"""
import functools
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from opentelemetry import baggage, metrics, trace
from opentelemetry import context as otel_context
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from services.common.events import enqueue_event  # noqa: E402

DEFAULT_ENDPOINT = "http://127.0.0.1:4318"

# URLs we never trace: health checks, Prometheus scraping, our own event plumbing and the
# long-lived live streams. (Comma-separated regular expressions.)
EXCLUDED_URLS = "health,metrics,internal/events,events/,api/logs,api/traffic"

_configured = False

# ---------------------------------------------------------------- the X-ray page's live span feed
# The tracking flag: the gateway puts this in OpenTelemetry BAGGAGE (key/value pairs that travel with a
# request in the `baggage` HTTP header, just like `traceparent`) for requests started from the X-ray page.
# Services downstream see it and know "this trace is being watched". Everything else (load tests, health
# checks) is not watched, so it costs nothing.
XRAY_BAGGAGE_KEY = "xray_request"
_ALLOWED_ATTRIBUTES = ("http.method", "http.route", "http.target", "http.url", "http.status_code",
                       "db.", "gen_ai.", "shopsense.", "langsmith.")  # allow-list: no headers, no bodies
_MAX_VALUE_CHARS = 400


def _clip(value: Any) -> Any:
    return value[:_MAX_VALUE_CHARS] + "…" if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS else value


class XraySpanProcessor(SpanProcessor):
    """Turns each span of a tracked request into live events: span_start when it begins, span_end when it ends.

    This is what lets the X-ray page draw EXACTLY what OpenTelemetry recorded, including spans created by
    libraries we never wrote code for (HTTP calls, database queries), instead of relying on hand-written reports.
    """

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        self.sink: Callable[[dict], None] = enqueue_event   # where events go; the gateway swaps in a local one
        self.emit_untracked = False                         # the gateway also wants spans it cannot yet attribute
        self._tracked: OrderedDict[str, str] = OrderedDict()  # trace id -> request id, for traces we know are watched
        self._lock = threading.Lock()

    def on_start(self, span, parent_context=None) -> None:
        trace_id = format(span.get_span_context().trace_id, "032x")
        flag = baggage.get_baggage(XRAY_BAGGAGE_KEY, parent_context or otel_context.get_current())
        with self._lock:
            if flag:
                self._tracked[trace_id] = str(flag)
                while len(self._tracked) > 500:
                    self._tracked.popitem(last=False)
            request_id = self._tracked.get(trace_id)
        if request_id or self.emit_untracked:
            self._emit("span_start", span, trace_id, request_id)

    def on_end(self, span) -> None:
        trace_id = format(span.get_span_context().trace_id, "032x")
        with self._lock:
            request_id = self._tracked.get(trace_id)
        if request_id or self.emit_untracked:
            self._emit("span_end", span, trace_id, request_id)

    def _emit(self, kind: str, span, trace_id: str, request_id: str | None) -> None:
        parent = span.parent
        payload: dict[str, Any] = {
            "span_id": format(span.get_span_context().span_id, "016x"),
            "parent_id": format(parent.span_id, "016x") if parent else None,
            "name": span.name, "service": self.service_name, "span_kind": span.kind.name,
            "start_ns": span.start_time,
            "attrs": {k: _clip(v) for k, v in (span.attributes or {}).items() if k.startswith(_ALLOWED_ATTRIBUTES)},
        }
        if kind == "span_end":
            payload["end_ns"] = span.end_time
            payload["status"] = span.status.status_code.name           # UNSET, OK or ERROR
            payload["status_message"] = _clip(span.status.description or "")
            payload["exceptions"] = [
                {"type": e.attributes.get("exception.type"), "message": _clip(str(e.attributes.get("exception.message", "")))}
                for e in (span.events or ()) if e.name == "exception"]
            payload["attrs"] = {k: _clip(v) for k, v in (span.attributes or {}).items() if k.startswith(_ALLOWED_ATTRIBUTES)}
        when_ns = span.start_time if kind == "span_start" else span.end_time
        try:
            self.sink({"request_id": request_id, "otel_trace_id": trace_id, "otel_span_id": payload["span_id"],
                       "ts": when_ns / 1e9, "component": self.service_name, "kind": kind, "caller": None,
                       "span_id": None, "duration_ms": None, "payload": payload})
        except Exception:  # noqa: BLE001 - the live feed must never break the request being traced
            pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


_xray = XraySpanProcessor("unset")  # given its real service name in setup_telemetry


def set_span_sink(sink: Callable[[dict], None], emit_untracked: bool = False) -> None:
    """Send span events to `sink` instead of to the gateway over HTTP (the gateway itself uses this)."""
    _xray.sink = sink
    _xray.emit_untracked = emit_untracked


@contextmanager
def track_request(request_id: str):
    """Mark everything done inside this block (and everything downstream of it) as a watched request."""
    token = otel_context.attach(baggage.set_baggage(XRAY_BAGGAGE_KEY, request_id))
    try:
        yield
    finally:
        otel_context.detach(token)


def setup_telemetry(service_name: str) -> None:
    """Create the tracer, the exporter, and switch on the automatic instrumentation.

    Call once, as early as possible in each service's main.py (before it makes HTTP calls).
    """
    global _configured
    if _configured:
        return
    _configured = True
    _xray.service_name = service_name

    # `localhost` -> 127.0.0.1 for the same IPv6 reason explained in common/config.py.
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT).replace("//localhost", "//127.0.0.1")

    # The Resource says WHO is producing the telemetry. `service.name` is how Tempo and Grafana
    # tell the gateway's spans from the orders service's spans.
    base = endpoint.rstrip("/")
    resource = Resource.create({"service.name": service_name, "deployment.environment": "local"})

    # Traces: finished spans are batched and sent to the Collector.
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
    # The X-ray page's live feed: a second processor that reports every span of a TRACKED request
    # straight to the gateway (see XraySpanProcessor below).
    provider.add_span_processor(_xray)
    trace.set_tracer_provider(provider)

    # Metrics: a reader wakes up every 5 s, collects the current value of every counter/histogram
    # and sends them to the Collector. (Prometheus does the pulling; we PUSH to the Collector.)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics"), export_interval_millis=5000)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    # Logs: Python's normal `logging` module, bridged into OpenTelemetry.
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs")))
    set_logger_provider(log_provider)
    _setup_logging(log_provider)

    HTTPXClientInstrumentor().instrument()   # spans for outgoing httpx calls + traceparent header
    SQLite3Instrumentor().instrument()       # spans for database queries


def _setup_logging(log_provider: LoggerProvider) -> None:
    """Send every log record (INFO and above) to the console AND to OpenTelemetry."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 1) Console: so you still see logs in the service's window. Uvicorn already prints its own
    #    lines, so we skip its loggers here to avoid every line appearing twice.
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console.addFilter(lambda record: not record.name.startswith("uvicorn"))
    root.addHandler(console)

    # 2) OpenTelemetry: this handler turns each record into an OTel log, stamped with the trace id
    #    and span id of whatever span is active at that moment. That stamp is what links logs to traces.
    otel = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
    #    Never ship OpenTelemetry's own error messages through OpenTelemetry: if the Collector were
    #    down, "failed to export" would itself be exported, fail, and log again, forever.
    otel.addFilter(lambda record: not record.name.startswith("opentelemetry"))
    root.addHandler(otel)

    # Chatty libraries: their INFO lines (one per HTTP call!) would drown the useful logs.
    # ("google_genai" is Google's Gemini SDK: it logs "AFC is enabled..." before every single call.)
    for noisy in ("httpx", "httpcore", "urllib3", "opentelemetry", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def instrument_app(app) -> None:
    """Create a span for every incoming request to this FastAPI app."""
    # exclude_spans drops the low-level "http send/receive" spans, which are noise for our purposes
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS, exclude_spans=["receive", "send"])


# ---------------------------------------------------------------- helpers for manual spans
def get_tracer() -> trace.Tracer:
    return trace.get_tracer("shopsense")


def current_trace_id() -> str | None:
    """The id of the trace we are inside right now, as 32 hex characters (None if there isn't one)."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def set_span_attributes(attributes: dict[str, Any]) -> None:
    """Attach key/value details to the span we are currently inside."""
    span = trace.get_current_span()
    for key, value in attributes.items():
        span.set_attribute(key, value)


def traced(span_name: str) -> Callable:
    """Decorator: run the function inside a span called `span_name`.

    If the function raises, the span is marked as an error and records the exception.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with get_tracer().start_as_current_span(span_name):
                return func(*args, **kwargs)

        return wrapper

    return decorator
