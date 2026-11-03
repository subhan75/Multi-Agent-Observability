"""Agents service: wraps the LangGraph graph in an HTTP endpoint (port 8003)."""
import uuid

from dotenv import load_dotenv

load_dotenv()  # must run before the graph module reads env vars

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from services.common.telemetry import instrument_app, setup_telemetry  # noqa: E402

setup_telemetry("agents")  # before importing the graph, which creates HTTP clients at import time

from services.agents import prompts  # noqa: E402
from services.agents.graph import build_graph, current_model_name  # noqa: E402
from services.common.events import install_event_middleware, request_id_var  # noqa: E402
from services.common.metrics import install_metrics  # noqa: E402
from services.common.telemetry import current_trace_id, set_span_attributes  # noqa: E402

app = FastAPI(title="agents-service")
install_event_middleware(app)  # adopt the gateway's X-Request-ID for every event we emit
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


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    # We choose the LangSmith run id ourselves (instead of letting LangChain invent one) so that we can
    # return it to the caller, attach it to the OpenTelemetry trace, and later attach user feedback to it.
    run_id = uuid.uuid4()
    trace_id = current_trace_id()
    set_span_attributes({"langsmith.run_id": str(run_id), "shopsense.prompt_version": prompts.PROMPT_VERSION})

    config = {
        "run_id": run_id,
        "run_name": "support_request",       # the name shown at the top of the run tree in LangSmith
        "tags": ["shopsense", f"prompt:{prompts.PROMPT_VERSION}"],
        # Metadata is searchable in LangSmith. otel_trace_id links a LangSmith run to its Tempo trace.
        "metadata": {"request_id": request_id_var.get(), "otel_trace_id": trace_id,
                     "model": current_model_name(), "prompt_version": prompts.PROMPT_VERSION},
    }
    final = graph.invoke({"question": req.question}, config=config)
    return AskResponse(answer=final["answer"], steps=final.get("steps", 0), run_id=str(run_id),
                       prompt_version=prompts.PROMPT_VERSION, visited=final.get("visited", []),
                       tools_used=final.get("tools_used", []))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
