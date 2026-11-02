"""Gateway: public entry point (port 8000), event hub, and host of the X-ray UI."""
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.common.config import service_url
from services.common.events import REQUEST_ID_HEADER, make_event
from services.common.metrics import install_metrics
from services.common.telemetry import current_trace_id, instrument_app, setup_telemetry
from services.gateway.store import EventStore

AGENTS_URL = service_url("AGENTS_URL", 8003)
UI_DIR = Path(__file__).resolve().parents[2] / "ui"



@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=120)  # reused for every request (see orders/main.py)
    yield
    await app.state.http.aclose()


app = FastAPI(title="gateway", lifespan=lifespan)
install_metrics(app)
setup_telemetry("gateway")
instrument_app(app)
store = EventStore()


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    # The browser may pick the id first so it can open the live stream before asking.
    request_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{4,40}$")


@app.post("/support")
async def support(body: Question) -> dict:
    request_id = body.request_id or uuid.uuid4().hex[:8]
    store.get_or_create(request_id, body.question)
    span_id = uuid.uuid4().hex[:8]
    trace_id = current_trace_id()  # the OpenTelemetry trace id of this request (lets the UI link to Grafana)
    started = time.perf_counter()

    store.add(make_event(request_id, "gateway", "step_start",
                         {"question": body.question, "summary": body.question, "trace_id": trace_id},
                         caller="user", span_id=span_id))
    try:
        resp = await app.state.http.post(f"{AGENTS_URL}/ask", json={"question": body.question},
                                         headers={REQUEST_ID_HEADER: request_id})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        store.add(make_event(request_id, "gateway", "step_error", {"error": repr(exc)},
                             duration_ms=elapsed, span_id=span_id))
        store.add(make_event(request_id, "gateway", "request_error", {"error": repr(exc)}))
        raise HTTPException(status_code=502, detail="Agents service failed") from exc

    result = resp.json()
    elapsed = (time.perf_counter() - started) * 1000
    store.add(make_event(request_id, "gateway", "step_end", {"summary": "answer returned"},
                         duration_ms=elapsed, span_id=span_id))
    store.add(make_event(request_id, "gateway", "request_done",
                         {"answer": result["answer"], "agent_steps": result["steps"], "trace_id": trace_id}, duration_ms=elapsed))
    return {"request_id": request_id, "trace_id": trace_id, **result}


@app.post("/internal/events")
async def receive_event(event: dict) -> dict[str, str]:
    """Other services POST their step events here."""
    store.add(event)
    return {"status": "ok"}


@app.get("/events/{request_id}/stream")
async def stream_events(request_id: str) -> StreamingResponse:
    return StreamingResponse(
        store.stream(request_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/requests")
def list_requests() -> list[dict]:
    return store.summaries()


@app.get("/requests/{request_id}/events")
def request_events(request_id: str) -> list[dict]:
    record = store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown request")
    return record.events


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Mounted last so the API routes above take priority over the static catch-all.
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
