"""OpenTelemetry tracing (Phase 2) and metrics (Phase 3), shared by every service.

What this gives us:
  * AUTOMATIC spans for every incoming web request (FastAPI), every outgoing HTTP call (httpx,
    which also covers the calls the Gemini SDK makes) and every SQLite query.
  * The W3C `traceparent` header, added to outgoing calls and read from incoming ones, so one
    request keeps the SAME trace id across the gateway, agents, orders and inventory services.
    (This is the professional version of our hand-made X-Request-ID header.)
  * Helpers for MANUAL spans around the things OTel cannot guess: agent steps, LLM calls, tools.

Spans are batched and sent to the OpenTelemetry Collector, which forwards them to Tempo.
Metrics (counters, histograms) are sent to the same Collector every 5 seconds; it exposes them on
a page that Prometheus scrapes. If the Collector is down, services keep working; data is dropped.
"""
import functools
import os
from collections.abc import Callable
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
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

    HTTPXClientInstrumentor().instrument()   # spans for outgoing httpx calls + traceparent header
    SQLite3Instrumentor().instrument()       # spans for database queries


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
