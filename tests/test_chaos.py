"""Injected problems: only when enabled, they travel between services, and they break the right things."""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.agents import mock_llm
from services.agents.graph import build_graph
from services.common import chaos, db
from services.common.events import current_headers


@pytest.fixture
def chaos_on(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "1")


@pytest.fixture
def set_scenario():
    """Set the per-request scenario for the duration of one test."""
    tokens = []

    def _set(name):
        tokens.append(chaos.chaos_var.set(name))

    yield _set
    for token in reversed(tokens):
        chaos.chaos_var.reset(token)


# ---------------------------------------------------------------------------------------- the switch
def test_parse_ignores_everything_when_disabled(monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)
    assert chaos.enabled() is False
    assert chaos.parse("slow_llm") is None


def test_parse_accepts_only_known_scenarios_when_enabled(chaos_on):
    assert chaos.parse("slow_llm") == "slow_llm"
    assert chaos.parse("drop_database") is None
    assert chaos.parse(None) is None


def _echo_app():
    app = FastAPI()
    chaos.install_chaos_middleware(app)

    @app.get("/seen")
    async def seen():
        return {"scenario": chaos.chaos_var.get(), "forward": current_headers()}

    return app


def test_middleware_adopts_the_header_and_forwards_it_to_the_next_hop(chaos_on):
    body = TestClient(_echo_app()).get("/seen", headers={"X-Chaos": "inventory_down"}).json()
    assert body["scenario"] == "inventory_down"
    assert body["forward"]["X-Chaos"] == "inventory_down"


def test_middleware_ignores_the_header_when_disabled(monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)
    body = TestClient(_echo_app()).get("/seen", headers={"X-Chaos": "inventory_down"}).json()
    assert body == {"scenario": None, "forward": {}}


def test_scenario_does_not_leak_into_the_next_request(chaos_on):
    client = TestClient(_echo_app())
    client.get("/seen", headers={"X-Chaos": "slow_llm"})
    assert client.get("/seen").json()["scenario"] is None


# ---------------------------------------------------------------------------------------- what each one breaks
def test_slow_llm_adds_delay(set_scenario, monkeypatch):
    slept = []
    monkeypatch.setattr(mock_llm.time, "sleep", slept.append)
    mock_llm._pause()
    assert slept == []                                  # healthy: MOCK_LLM_LATENCY_MS=0 means no delay
    set_scenario("slow_llm")
    mock_llm._pause()
    assert len(slept) == 1 and 1.5 <= slept[0] <= 2.5


def test_supervisor_loop_makes_the_guardrail_fire(fake_orders_service, set_scenario, monkeypatch):
    from services.agents import graph
    decisions = []
    monkeypatch.setattr(graph, "record_supervisor_decision", lambda choice, overridden: decisions.append((choice, overridden)))
    healthy = build_graph().invoke({"question": "Where is order 42, and can I return it?"})
    assert healthy["visited"] == ["order_agent", "policy_agent", "writer"] and (("writer", True) not in decisions)

    decisions.clear()
    set_scenario("supervisor_loop")
    broken = build_graph().invoke({"question": "Where is order 42, and can I return it?"})
    assert broken["visited"] == ["order_agent", "writer"]          # the policy question was never answered
    assert ("writer", True) in decisions                           # the guardrail had to override the model


def test_inventory_down_returns_503_but_only_when_injected(tmp_path, monkeypatch, set_scenario, chaos_on):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)                  # never touch the developer's real data/ folder
    from services.inventory.main import app
    with TestClient(app) as client:
        assert client.get("/stock/sku-pencil").status_code == 200
        response = client.get("/stock/sku-pencil", headers={"X-Chaos": "inventory_down"})
        assert response.status_code == 503
        assert client.get("/health", headers={"X-Chaos": "inventory_down"}).status_code == 200   # health stays up: that is the lesson
        assert client.get("/stock/sku-pencil").status_code == 200   # and the next request is healthy again


def test_inventory_ignores_the_header_when_chaos_is_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    from services.inventory.main import app
    with TestClient(app) as client:
        assert client.get("/stock/sku-pencil", headers={"X-Chaos": "inventory_down"}).status_code == 200


# ---------------------------------------------------------------------------------------- the gateway
def test_gateway_forwards_the_scenario_only_when_enabled(monkeypatch):
    from services.gateway import main as gateway
    answer = {"answer": "x", "steps": 1, "run_id": "r", "prompt_version": "p", "visited": [], "tools_used": [],
              "llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    seen = []

    def agents(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ask":                  # (/config is also asked, by /api/config)
            seen.append(request.headers.get("x-chaos"))
            return httpx.Response(200, json=answer)
        return httpx.Response(200, json={})

    with TestClient(gateway.app) as client:
        gateway.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(agents))
        monkeypatch.delenv("CHAOS_ENABLED", raising=False)
        client.post("/support", json={"question": "Where is order 42?", "chaos": "slow_llm"})
        monkeypatch.setenv("CHAOS_ENABLED", "1")
        client.post("/support", json={"question": "Where is order 42?", "chaos": "slow_llm"})
        advertised = client.get("/api/config").json()["chaos_scenarios"]
        assert client.post("/support", json={"question": "Where is order 42?", "chaos": "rm_rf"}).status_code == 422
    assert seen == [None, "slow_llm"]
    assert set(advertised) == {"slow_llm", "supervisor_loop", "inventory_down"}
