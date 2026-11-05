"""The mock model must behave like the real one from the graph's point of view (routing, tools, tokens)."""
import pytest

from services.agents import models
from services.agents.graph import build_graph
from services.agents.mock_llm import MockChatModel, decide_next_step
from services.common.metrics import new_usage_tally, request_usage_var
from services.common.pricing import cost_usd


# ---------------------------------------------------------------------------------------- choosing the mode
def test_default_mode_is_mock_without_a_key():
    assert models.default_mode() == "mock"          # conftest forces LLM_MODE=mock; remove it to test the key logic below


def test_mode_follows_the_api_key(monkeypatch):
    monkeypatch.delenv("LLM_MODE")
    assert models.default_mode() == "mock"
    monkeypatch.setenv("GOOGLE_API_KEY", "your-gemini-api-key")      # the placeholder from .env.example is not a key
    assert models.default_mode() == "mock"
    monkeypatch.setenv("GOOGLE_API_KEY", "a-real-looking-key")
    assert models.default_mode() == "live"
    assert models.live_available() is True


def test_requested_mode_wins_and_junk_is_ignored(monkeypatch):
    monkeypatch.delenv("LLM_MODE")
    monkeypatch.setenv("GOOGLE_API_KEY", "a-real-looking-key")
    assert models.resolve_mode("mock") == "mock"
    assert models.resolve_mode("LIVE") == "live"
    assert models.resolve_mode("nonsense") == "live"                 # falls back to the default
    assert models.resolve_mode(None) == "live"


def test_model_name_follows_the_mode(monkeypatch):
    models.llm_mode_var.set("mock")
    assert models.current_model_name() == "mock-llm"
    models.llm_mode_var.set("live")
    monkeypatch.setenv("GEMINI_MODEL", "")
    assert models.current_model_name() == models.DEFAULT_MODEL       # an empty GEMINI_MODEL means the default
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    assert models.current_model_name() == "gemini-3.8-flash"


# ---------------------------------------------------------------------------------------- the model's contracts
def test_plain_invoke_returns_a_message_with_usage():
    reply = MockChatModel().invoke("Write a short, friendly reply to the customer using only the information below.\n"
                                   "If something is missing, say so honestly instead of guessing.\n\n"
                                   "Question: hi\nOrder info: (not looked up)\nPolicy info: (not looked up)")
    assert reply.text
    assert reply.usage_metadata["input_tokens"] > 0 and reply.usage_metadata["output_tokens"] > 0
    assert reply.response_metadata["model_name"] == "mock-llm"


def test_structured_output_matches_what_graph_ask_expects():
    from services.agents.graph import Decision
    out = MockChatModel().with_structured_output(Decision, include_raw=True).invoke(
        "Already gathered:\norder info: (none)\npolicy info: (none)\n\nNever pick an agent whose info is already gathered. "
        "Customer question: Where is order 42?")
    assert out["parsing_error"] is None
    assert out["parsed"].next_step == "order_agent"
    assert out["raw"].usage_metadata["total_tokens"] > 0


def test_bind_tools_is_accepted():
    model = MockChatModel()
    assert model.bind_tools([object()], tool_choice="auto") is model


@pytest.mark.parametrize("question, order_info, policy_info, expected", [
    ("Where is order 42?", "(none)", "(none)", "order_agent"),
    ("Where is order 42?", "Order 42 shipped", "(none)", "writer"),
    ("Where is order 42, and can I return it?", "Order 42 shipped", "(none)", "policy_agent"),
    ("How long does express shipping take?", "(none)", "(none)", "policy_agent"),
    ("How long does express shipping take?", "(none)", "Express: 1-2 days", "writer"),
    ("What is the weather today?", "(none)", "(none)", "writer"),
])
def test_supervisor_rules(question, order_info, policy_info, expected):
    prompt = f"Already gathered:\norder info: {order_info}\npolicy info: {policy_info}\n\nNever pick an agent. Customer question: {question}"
    assert decide_next_step(prompt) == expected


# ---------------------------------------------------------------------------------------- the whole graph, in mock mode
def ask(question: str) -> dict:
    return build_graph().invoke({"question": question})


def test_order_and_policy_question(fake_orders_service):
    final = ask("Where is order 42, and can I return it?")
    assert final["visited"] == ["order_agent", "policy_agent", "writer"]
    assert final["tools_used"] == ["get_order"]
    assert "shipped" in final["answer"] and "30 days" in final["answer"]


def test_order_only_question(fake_orders_service):
    final = ask("Is the product in order 43 in stock?")
    assert final["visited"] == ["order_agent", "writer"]
    assert "backordered" in final["answer"]


def test_policy_only_question(fake_orders_service):
    final = ask("How long does express shipping take?")
    assert final["visited"] == ["policy_agent", "writer"]
    assert final.get("tools_used", []) == []
    assert "1-2 business days" in final["answer"]


def test_unknown_order_is_handled_honestly(fake_orders_service):
    final = ask("Where is order 999?")
    assert final["visited"] == ["order_agent", "writer"]
    assert "No order found" in final["answer"]


def test_off_topic_goes_straight_to_the_writer(fake_orders_service):
    final = ask("What is the weather like today?")
    assert final["visited"] == ["writer"]
    assert "order" in final["answer"].lower()


def test_tokens_and_cost_are_counted_per_question(fake_orders_service):
    tally = new_usage_tally()
    token = request_usage_var.set(tally)
    try:
        ask("Where is order 42, and can I return it?")
    finally:
        request_usage_var.reset(token)
    assert tally["llm_calls"] >= 5                      # 3 supervisor + 2 order-agent + policy + writer in the real flow
    assert tally["input_tokens"] > 0 and tally["output_tokens"] > 0
    assert tally["cost_usd"] > 0
    assert cost_usd("mock-llm", 1_000_000, 0) == pytest.approx(0.10)
