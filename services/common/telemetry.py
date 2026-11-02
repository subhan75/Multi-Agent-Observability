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
from collections.abc import Callable
from typing import Any

from opentelemetry import metrics, trace
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
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

DEFAULT_ENDPOINT = "http://127.0.0.1:4318"

# URLs we never trace: health checks, Prometheus scraping, our own event plumbing and the
# long-lived live streams. (Comma-separated regular expressions.)
EXCLUDED_URLS = "health,metrics,internal/events,events/"

_configured = False


def setup_telemetry(service_name: str) -> None:
    """Create the tracer, the exporter, and switch on the automatic instrumentation.

    Call once, as early as possible in each service's main.py (before it makes HTTP calls).
    """
    global _configured
    if _configured:
        return
    _configured = True

    # `localhost` -> 127.0.0.1 for the same IPv6 reason explained in common/config.py.
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT).replace("//localhost", "//127.0.0.1")

    # The Resource says WHO is producing the telemetry. `service.name` is how Tempo and Grafana
    # tell the gateway's spans from the orders service's spans.
    base = endpoint.rstrip("/")
    resource = Resource.create({"service.name": service_name, "deployment.environment": "local"})

    # Traces: finished spans are batched and sent to the Collector.
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
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
    for noisy in ("httpx", "httpcore", "urllib3", "opentelemetry"):
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
