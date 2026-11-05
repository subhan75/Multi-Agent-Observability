"""The Mock/Live choice must travel browser -> gateway -> agents, and the page config must degrade gracefully."""
import httpx
import pytest
from fastapi.testclient import TestClient

from services.gateway import main as gateway

AGENT_ANSWER = {"answer": "hi", "steps": 1, "run_id": "run-1", "prompt_version": "abc12345", "visited": ["writer"],
                "tools_used": [], "llm_calls": 1, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0, "llm_mode": "mock"}


@pytest.fixture
def client():
    with TestClient(gateway.app) as test_client:      # the `with` runs the app's start-up (creates app.state.http)
        yield test_client


def test_config_falls_back_to_mock_when_agents_are_unreachable(client, monkeypatch):
    monkeypatch.setattr(gateway, "AGENTS_URL", "http://127.0.0.1:9")      # nothing listens on port 9
    body = client.get("/api/config").json()
    assert body["live_available"] is False
    assert body["default_mode"] == "mock"
    assert body["grafana_url"].startswith("http")


def test_config_passes_through_what_the_agents_report(client):
    def agents(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/config"
        return httpx.Response(200, json={"live_available": True, "default_mode": "live", "live_model": "gemini-x"})

    gateway.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(agents))
    body = client.get("/api/config").json()
    assert body["live_available"] is True and body["default_mode"] == "live" and body["live_model"] == "gemini-x"


@pytest.mark.parametrize("sent, header", [("mock", "mock"), ("live", "live"), (None, None)])
def test_llm_mode_is_forwarded_as_a_header(client, sent, header):
    seen = {}

    def agents(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("x-llm-mode")
        return httpx.Response(200, json=AGENT_ANSWER)

    gateway.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(agents))
    payload = {"question": "Where is order 42?"}
    if sent:
        payload["llm_mode"] = sent
    response = client.post("/support", json=payload)
    assert response.status_code == 200
    assert seen["header"] == header
    assert response.json()["llm_mode"] == "mock"        # the agents' answer is passed back to the browser


def test_unknown_mode_is_rejected(client):
    assert client.post("/support", json={"question": "Where is order 42?", "llm_mode": "turbo"}).status_code == 422
