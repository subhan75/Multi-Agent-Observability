"""Shared test setup: everything runs offline, with the mock model, instantly."""
import json
import os

import httpx
import pytest

# The tests never talk to a collector; switching the OpenTelemetry SDK off avoids slow, noisy "cannot export" retries.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from services.agents import models  # noqa: E402


@pytest.fixture(autouse=True)
def offline_mock_mode(monkeypatch):
    """Every test starts in mock mode with no delay and no API key, whatever the developer's own environment says."""
    monkeypatch.setenv("MOCK_LLM_LATENCY_MS", "0")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    token = models.llm_mode_var.set(None)
    yield
    models.llm_mode_var.reset(token)


ORDERS = {
    42: {"id": 42, "customer": "alice", "sku": "sku-pencil", "quantity": 3, "status": "shipped", "placed_on": "2026-10-01", "in_stock_now": 120},
    43: {"id": 43, "customer": "bob", "sku": "sku-notebook", "quantity": 1, "status": "backordered", "placed_on": "2026-10-10", "in_stock_now": 0},
}


@pytest.fixture
def fake_orders_service(monkeypatch):
    """Replace the HTTP client the order tool uses with one that answers from memory (the real service is not running)."""
    def handler(request: httpx.Request) -> httpx.Response:
        order_id = int(request.url.path.rsplit("/", 1)[-1])
        if order_id in ORDERS:
            return httpx.Response(200, content=json.dumps(ORDERS[order_id]))
        return httpx.Response(404, json={"detail": "Order not found"})

    from services.agents import tools
    monkeypatch.setattr(tools, "_http", httpx.Client(transport=httpx.MockTransport(handler)))
