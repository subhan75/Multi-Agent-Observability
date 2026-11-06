"""The traffic button must be safe: mock-only, bounded, one batch at a time, and invisible to the X-ray history."""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from services.gateway import main as gateway
from services.gateway import traffic as traffic_module

AGENT_ANSWER = {"answer": "hi", "steps": 1, "run_id": "r", "prompt_version": "p", "visited": [], "tools_used": [],
                "llm_calls": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0, "llm_mode": "mock"}


@pytest.fixture
def client():
    with TestClient(gateway.app) as test_client:
        gateway.traffic.running = False
        yield test_client


def wait_until_idle(client, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = client.get("/api/traffic").json()
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("traffic batch did not finish")


def install_fake_backends(agent_delay=0.0, agent_status=200):
    """Agents and orders/inventory are replaced by in-memory fakes; returns the lists of what they received."""
    asks, directs = [], []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ask":
            asks.append(request.headers.get("x-llm-mode"))
            if agent_delay:
                await asyncio.sleep(agent_delay)
            return httpx.Response(agent_status, json=AGENT_ANSWER)
        directs.append(request.url.path)
        return httpx.Response(404 if "999" in request.url.path or "nope" in request.url.path else 200, json={})

    gateway.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return asks, directs


def test_a_batch_sends_only_mock_questions_and_direct_calls(client):
    asks, directs = install_fake_backends()
    assert client.post("/api/traffic?count=5").status_code == 202
    status = wait_until_idle(client)
    assert status == {"running": False, "total": 5, "done": 5, "failed": 0}
    assert asks == ["mock"] * 5                                   # never the real model, whatever the caller says
    assert len(directs) == 5 * traffic_module.DIRECT_CALLS_PER_QUESTION


def test_batches_are_not_recorded_as_xray_requests(client):
    install_fake_backends()
    before = len(client.get("/requests").json())
    client.post("/api/traffic?count=4")
    wait_until_idle(client)
    assert len(client.get("/requests").json()) == before


def test_second_batch_is_refused_while_one_is_running(client):
    install_fake_backends(agent_delay=0.3)
    assert client.post("/api/traffic?count=3").status_code == 202
    assert client.post("/api/traffic?count=3").status_code == 409
    wait_until_idle(client)
    assert client.post("/api/traffic?count=1").status_code == 202      # allowed again afterwards
    wait_until_idle(client)


@pytest.mark.parametrize("count", [0, traffic_module.MAX_COUNT + 1, -3])
def test_count_is_bounded(client, count):
    assert client.post(f"/api/traffic?count={count}").status_code == 422


def test_failures_are_counted_not_fatal(client):
    install_fake_backends(agent_status=500)
    client.post("/api/traffic?count=4")
    status = wait_until_idle(client)
    assert status["done"] == 4 and status["failed"] == 4 and status["running"] is False


def test_concurrency_never_exceeds_the_limit(client):
    in_flight, peak = 0, 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        if request.url.path == "/ask":
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return httpx.Response(200, json=AGENT_ANSWER)
        return httpx.Response(200, json={})

    gateway.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.post("/api/traffic?count=9")
    wait_until_idle(client)
    assert 1 < peak <= traffic_module.CONCURRENCY
