"""Agents service: wraps the LangGraph graph in an HTTP endpoint (port 8003)."""
from dotenv import load_dotenv

load_dotenv()  # must run before the graph module reads env vars

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from services.agents.graph import build_graph  # noqa: E402
from services.common.events import install_event_middleware  # noqa: E402
from services.common.metrics import install_metrics  # noqa: E402

app = FastAPI(title="agents-service")
install_event_middleware(app)  # adopt the gateway's X-Request-ID for every event we emit
install_metrics(app)
graph = build_graph()  # compiled once, reused for every request


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    steps: int


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    final = graph.invoke({"question": req.question})
    return AskResponse(answer=final["answer"], steps=final.get("steps", 0))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
