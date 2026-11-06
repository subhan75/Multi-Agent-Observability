"""Agents service: wraps the LangGraph graph in an HTTP endpoint (port 8003)."""
import logging
import uuid

from dotenv import load_dotenv

load_dotenv()  # must run before the graph module reads env vars

from fastapi import FastAPI, Header  # noqa: E402
from langsmith import Client  # noqa: E402
from langsmith.utils import tracing_is_enabled  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from services.common.telemetry import instrument_app, setup_telemetry  # noqa: E402

setup_telemetry("agents")  # before importing the graph, which creates HTTP clients at import time

from services.agents import prompts  # noqa: E402
from services.agents.graph import build_graph, current_model_name  # noqa: E402
from services.agents.models import default_mode, live_available, live_model_name, llm_mode_var, resolve_mode  # noqa: E402
from services.common.chaos import install_chaos_middleware  # noqa: E402
from services.common.events import install_event_middleware, request_id_var  # noqa: E402
from services.common.metrics import install_metrics, new_usage_tally, record_feedback, request_usage_var  # noqa: E402
from services.common.telemetry import current_trace_id, set_span_attributes  # noqa: E402

logger = logging.getLogger(__name__)

app = FastAPI(title="agents-service")
install_event_middleware(app)  # adopt the gateway's X-Request-ID for every event we emit
install_chaos_middleware(app)  # optional injected problems (off unless CHAOS_ENABLED=1)
install_metrics(app)
instrument_app(app)
graph = build_graph()  # compiled once, reused for every request


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    steps: int
    run_id: str                 # the LangSmith run id of this question (see Phase 6)
    prompt_version: str
    visited: list[str] = []     # which workers ran, in order
    tools_used: list[str] = []  # which tools the order agent called
    llm_calls: int = 0          # model calls made for this question (including the order agent's inner ones)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0       # ESTIMATED, from services/common/model_prices.json
    llm_mode: str = "live"      # "mock" (built-in fake model) or "live" (real Gemini)


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, x_llm_mode: str | None = Header(default=None)) -> AskResponse:
    # Mock or live? The browser's switch arrives here as a header; no header means the default (live if a key exists).
    mode = resolve_mode(x_llm_mode)
    llm_mode_var.set(mode)
    # We choose the LangSmith run id ourselves (instead of letting LangChain invent one) so that we can
    # return it to the caller, attach it to the OpenTelemetry trace, and later attach user feedback to it.
    run_id = uuid.uuid4()
    trace_id = current_trace_id()
    set_span_attributes({"langsmith.run_id": str(run_id), "shopsense.prompt_version": prompts.PROMPT_VERSION, "shopsense.llm_mode": mode})

    config = {
        "run_id": run_id,
        "run_name": "support_request",       # the name shown at the top of the run tree in LangSmith
        "tags": ["shopsense", f"prompt:{prompts.PROMPT_VERSION}"],
        # Metadata is searchable in LangSmith. otel_trace_id links a LangSmith run to its Tempo trace.
        "metadata": {"request_id": request_id_var.get(), "otel_trace_id": trace_id,
                     "model": current_model_name(), "prompt_version": prompts.PROMPT_VERSION},
    }
    # Every model call during this question adds its tokens and cost to this dict (see record_llm_usage).
    usage = new_usage_tally()
    request_usage_var.set(usage)
    final = graph.invoke({"question": req.question}, config=config)
    set_span_attributes({"gen_ai.usage.input_tokens": usage["input_tokens"], "gen_ai.usage.output_tokens": usage["output_tokens"],
                         "shopsense.cost_usd": round(usage["cost_usd"], 6), "shopsense.llm_calls": usage["llm_calls"]})
    return AskResponse(answer=final["answer"], steps=final.get("steps", 0), run_id=str(run_id),
                       prompt_version=prompts.PROMPT_VERSION, visited=final.get("visited", []),
                       tools_used=final.get("tools_used", []), llm_calls=usage["llm_calls"],
                       input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                       cost_usd=round(usage["cost_usd"], 6), llm_mode=mode)


class FeedbackRequest(BaseModel):
    run_id: str
    score: int = Field(ge=0, le=1)  # 1 = thumbs up, 0 = thumbs down
    comment: str | None = Field(default=None, max_length=500)


class FeedbackResponse(BaseModel):
    recorded_in_langsmith: bool
    detail: str


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(req: FeedbackRequest) -> FeedbackResponse:
    """Attach a user's thumbs up/down to the LangSmith run that produced the answer.

    The Prometheus counter is always updated. Reaching LangSmith is best-effort: a user's click
    must never fail just because tracing is switched off or the network is down.
    """
    record_feedback(req.score)
    if not tracing_is_enabled():
        return FeedbackResponse(recorded_in_langsmith=False, detail="LangSmith tracing is off; feedback was counted in Prometheus only.")
    try:
        Client().create_feedback(run_id=req.run_id, key="user_score", score=req.score, comment=req.comment)
    except Exception as exc:  # noqa: BLE001 - any LangSmith/network failure is non-fatal here
        logger.warning("could not send feedback to LangSmith", extra={"run_id": req.run_id, "error_type": type(exc).__name__})
        return FeedbackResponse(recorded_in_langsmith=False, detail=f"LangSmith did not accept the feedback ({type(exc).__name__}).")
    logger.info("user feedback recorded", extra={"run_id": req.run_id, "score": req.score})
    return FeedbackResponse(recorded_in_langsmith=True, detail="Feedback attached to the LangSmith run.")


@app.get("/config")
def config() -> dict:
    """What the X-ray page needs to know: can it offer the real model, and which mode should it start in?"""
    return {"live_available": live_available(), "default_mode": default_mode(), "live_model": live_model_name() if live_available() else None}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
