"""A fake chat model, so the whole system can run with no API key and no cost.

Only the MODEL is fake. Everything around it is real: the HTTP calls between services, the SQLite queries,
the tool calls, and every span, metric and log line. So the traces you look at while learning are shaped
exactly like the ones a real model produces.

How it "thinks" is a handful of plain rules over the prompt text (no AI involved):
  * supervisor    - needs an order lookup? then the order agent; needs policy? the policy agent; else the writer
  * order agent   - first turn: asks to run the `get_order` tool with the number found in the question;
                    after the tool answers: summarises the order
  * policy agent  - quotes the policy excerpts it was given
  * writer        - stitches the order and policy facts into a short reply

It also sleeps a little (so the waterfall has something to show) and reports token counts (about 4 characters
per token) so the token and cost panels have numbers. The delay is set by MOCK_LLM_LATENCY_MS, e.g. "300-1200"
(the default), "50", or "0" for none (tests).
"""
import json
import os
import random
import re
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from services.agents import prompts

DEFAULT_LATENCY_MS = "300-1200"
_ORDER_WORDS = re.compile(r"\border\b|\bpackage\b|\bparcel\b|\btrack|\bin stock\b|\bstock\b", re.I)
_POLICY_WORDS = re.compile(r"return|refund|shipping|cancel|exchange|policy|policies|how long|deliver", re.I)
_POLICY_PREFIX = prompts.POLICY_AGENT.split("{")[0]
_WRITER_PREFIX = prompts.WRITER.split("{")[0]


# ---------------------------------------------------------------------------------------- helpers
def _pause() -> None:
    """Sleep a random time inside the configured range, like a real model call takes time."""
    spec = os.getenv("MOCK_LLM_LATENCY_MS", DEFAULT_LATENCY_MS).strip()
    try:
        low, _, high = spec.partition("-")
        low_ms, high_ms = float(low), float(high or low)
    except ValueError:
        low_ms, high_ms = 300.0, 1200.0
    if high_ms > 0:
        time.sleep(random.uniform(min(low_ms, high_ms), max(low_ms, high_ms)) / 1000)


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _text_of(value: Any) -> str:
    """Plain text of whatever LangChain hands a model: a string, a message, a list of messages, a prompt value."""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseMessage):
        return value.text if hasattr(value, "text") and isinstance(value.text, str) else str(value.content)
    if hasattr(value, "to_messages"):
        return _text_of(value.to_messages())
    if isinstance(value, (list, tuple)):
        return "\n".join(_text_of(item) for item in value)
    return str(value)


def _field(text: str, start: str, end: str) -> str:
    """The text between two markers (empty string if missing)."""
    match = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.S)
    return match.group(1).strip() if match else ""


def _order_number(question: str) -> int | None:
    match = re.search(r"order\s*(?:number|no\.?|id|#)?\s*#?(\d+)", question, re.I) or re.search(r"#?\b(\d{1,6})\b", question)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------------------- the "brains"
def decide_next_step(prompt: str) -> str:
    """The supervisor's choice, read from the filled-in supervisor prompt."""
    order_info = _field(prompt, "order info:", "\npolicy info:")
    policy_info = _field(prompt, "policy info:", "\n\nNever pick")
    question = prompt.rsplit("Customer question:", 1)[-1].strip()
    order_done, policy_done = order_info not in ("", "(none)"), policy_info not in ("", "(none)")
    wants_order = bool(_ORDER_WORDS.search(question)) or bool(re.search(r"\d", question))
    wants_policy = bool(_POLICY_WORDS.search(question))
    if wants_order and not order_done:
        return "order_agent"
    if wants_policy and not policy_done:
        return "policy_agent"
    return "writer"


def _summarise_order(tool_text: str) -> str:
    try:
        order = json.loads(tool_text)
        stock = order.get("in_stock_now")
        stock_text = f" Stock right now: {stock} units." if stock is not None else ""
        return (f"Order {order['id']} ({order['customer']}): {order['quantity']} x {order['sku']}, "
                f"status {order['status']}, placed on {order['placed_on']}.{stock_text}")
    except (ValueError, KeyError, TypeError):
        return tool_text.strip() or "The order lookup returned nothing."


def _policy_reply(prompt: str) -> str:
    excerpts = _field(prompt, "state the rules relevant to the question.", "\n\nQuestion:")
    lines = [line.strip() for line in excerpts.splitlines() if line.strip()]
    return "Relevant policy: " + " ".join(line.lstrip("# ").rstrip() + ("" if line.rstrip().endswith(".") else ":") for line in lines)[:600]


def _writer_reply(prompt: str) -> str:
    order_info = _field(prompt, "Order info:", "\nPolicy info:")
    policy_info = prompt.rsplit("Policy info:", 1)[-1].strip()
    have_order = order_info and order_info != "(not looked up)"
    have_policy = policy_info and policy_info != "(not looked up)"
    if not have_order and not have_policy:
        return "Hi! I can help with order status and our shipping, returns and cancellation policies. Could you tell me which one you need?"
    parts = ["Hi there! Thanks for reaching out."]
    if have_order:
        parts.append(order_info if order_info.lower().startswith(("order", "no order")) else f"Here is what I found: {order_info}")
    if have_policy:
        parts.append(policy_info.replace("Relevant policy: ", "Our policy: ", 1))
    parts.append("Is there anything else I can help with?")
    return " ".join(parts)


# ---------------------------------------------------------------------------------------- the model
class MockChatModel(BaseChatModel):
    """Drop-in replacement for ChatGoogleGenerativeAI that follows the rules described at the top of this file."""

    @property
    def _llm_type(self) -> str:
        return "mock-llm"

    def _message(self, content: str, prompt: str, tool_calls: list[dict] | None = None) -> AIMessage:
        input_tokens, output_tokens = _tokens(prompt), _tokens(content or json.dumps(tool_calls or []))
        return AIMessage(content=content, tool_calls=tool_calls or [],
                         usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens,
                                         "total_tokens": input_tokens + output_tokens},
                         response_metadata={"model_name": "mock-llm"})

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        _pause()
        prompt = _text_of(messages)
        last = messages[-1]
        if isinstance(last, ToolMessage):                      # the order agent's tool just answered
            message = self._message(_summarise_order(_text_of(last)), prompt)
        elif prompt.lstrip().startswith(_POLICY_PREFIX) or _POLICY_PREFIX in prompt:
            message = self._message(_policy_reply(prompt), prompt)
        elif _WRITER_PREFIX in prompt:
            message = self._message(_writer_reply(prompt), prompt)
        else:                                                  # the order agent's first turn: ask for the tool
            number = _order_number(_text_of(last))
            if number is None:
                message = self._message("No order number was given, so I could not look anything up.", prompt)
            else:
                call = {"name": "get_order", "args": {"order_id": number}, "id": f"mock-{uuid.uuid4().hex[:8]}", "type": "tool_call"}
                message = self._message("", prompt, [call])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any) -> "MockChatModel":
        return self  # the rules above already know about the one tool; nothing needs binding

    def with_structured_output(self, schema: Any, *, include_raw: bool = False, **kwargs: Any) -> RunnableLambda:
        """Only the supervisor's `Decision` schema is needed: it has a single `next_step` field."""

        def run(value: Any) -> Any:
            _pause()
            prompt = _text_of(value)
            parsed = schema(next_step=decide_next_step(prompt))
            if not include_raw:
                return parsed
            raw = self._message(parsed.model_dump_json(), prompt)
            return {"raw": raw, "parsed": parsed, "parsing_error": None}

        return RunnableLambda(run)
