"""Observe the model calls that happen INSIDE a LangChain agent.

Our own code wraps every Gemini call it makes directly (see `_ask` in graph.py). But the order agent is
built with LangChain's `create_agent`, which makes its own model calls internally (one to decide to use
the tool, one to write the summary). We cannot wrap those, so we use a CALLBACK HANDLER: LangChain calls
these methods at the start and end of every model call, and we record what we see.

What gets recorded for each inner call: a count and duration (Prometheus), token usage and estimated
cost (Prometheus + the per-question tally), an X-ray event, and a log line.
"""
import logging
import time
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from services.agents.models import current_model_name
from services.common.events import emit_event
from services.common.metrics import record_llm_call, record_llm_usage

logger = logging.getLogger(__name__)


def _render(messages: list[list]) -> str:
    """The conversation sent to the model as plain text (for the X-ray; truncated downstream)."""
    return "\n".join(f"{getattr(m, 'type', 'message')}: {getattr(m, 'content', '')}" for m in (messages[0] if messages else []))


class AgentLLMRecorder(BaseCallbackHandler):
    """Records every model call made inside one agent. Create one per agent run; read the totals afterwards."""

    def __init__(self, caller: str) -> None:
        self.caller = caller
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self._started: dict[UUID, float] = {}
        self._prompts: dict[UUID, str] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs) -> None:
        self._started[run_id] = time.perf_counter()
        self._prompts[run_id] = _render(messages)

    def on_llm_end(self, response, *, run_id: UUID, **kwargs) -> None:
        elapsed = time.perf_counter() - self._started.pop(run_id, time.perf_counter())
        prompt = self._prompts.pop(run_id, "")
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None) or {}
        input_tokens, output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        model = current_model_name()

        record_llm_call(model, self.caller, elapsed, "ok")
        cost = record_llm_usage(model, self.caller, input_tokens, output_tokens)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

        # If the model asked to use a tool, say which; otherwise this call produced the final text.
        asked_for = [call["name"] for call in (getattr(message, "tool_calls", None) or [])]
        emit_event(
            "gemini", "llm_call",
            {"from": self.caller, "model": model, "prompt": prompt, "response": message.text or "",
             "requested_tools": asked_for, "input_tokens": input_tokens, "output_tokens": output_tokens,
             "cost_usd": round(cost, 6),
             "summary": f"{model} <- {self.caller}" + (f" (asks for tool {', '.join(asked_for)})" if asked_for else " (writes the summary)")},
            caller=self.caller, duration_ms=elapsed * 1000,
        )
        logger.info("llm call finished", extra={"agent": self.caller, "model": model, "duration_ms": round(elapsed * 1000),
                                                "input_tokens": input_tokens, "output_tokens": output_tokens,
                                                "requested_tools": ",".join(asked_for)})

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs) -> None:
        elapsed = time.perf_counter() - self._started.pop(run_id, time.perf_counter())
        self._prompts.pop(run_id, None)
        record_llm_call(current_model_name(), self.caller, elapsed, "error")
        logger.warning("llm call failed", extra={"agent": self.caller, "error_type": type(error).__name__})
