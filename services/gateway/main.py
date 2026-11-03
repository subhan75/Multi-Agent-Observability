"""Gateway: public entry point (port 8000), event hub, and host of the X-ray UI."""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Path as PathParam
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.common.config import service_url
from services.common.events import REQUEST_ID_HEADER, make_event
from services.common.metrics import install_metrics
from services.common.telemetry import current_trace_id, instrument_app, set_span_sink, setup_telemetry, track_request
from services.gateway.store import EventStore

logger = logging.getLogger(__name__)

AGENTS_URL = service_url("AGENTS_URL", 8003)
LOKI_URL = service_url("LOKI_URL", 3100)
# Log metadata that is plumbing, not information: hidden from the X-ray page.
_HIDDEN_LOG_FIELDS = {"code_file_path", "code_function_name", "code_line_number", "flags", "observed_timestamp", "scope_name",
                      "service_instance_id", "severity_number", "severity_text", "telemetry_sdk_language",
                      "telemetry_sdk_name", "telemetry_sdk_version", "trace_id", "span_id", "detected_level"}
UI_DIR = Path(__file__).resolve().parents[2] / "ui"



_loop: asyncio.AbstractEventLoop | None = None  # the web server's event loop, captured at startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    app.state.http = httpx.AsyncClient(timeout=120)  # reused for every request (see orders/main.py)
    yield
    await app.state.http.aclose()


app = FastAPI(title="gateway", lifespan=lifespan)
install_metrics(app)
setup_telemetry("gateway")
instrument_app(app)
store = EventStore()


def _span_sink(event: dict) -> None:
    """The gateway's own spans go straight into the store (no HTTP to itself).

    Spans end on whatever thread finished them, but the store is only safe on the event loop, so we hand
    the event over with call_soon_threadsafe instead of touching the store directly.
    """
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(store.add_span_event, event)


set_span_sink(_span_sink, emit_untracked=True)  # untracked too: the root span starts before we know its request id


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    # The browser may pick the id first so it can open the live stream before asking.
    request_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{4,40}$")


@app.post("/support")
async def support(body: Question) -> dict:
    request_id = body.request_id or uuid.uuid4().hex[:8]
    # track_request marks this request, and every span downstream of it, as "watched" for the X-ray page.
    with track_request(request_id):
        return await _answer_question(body, request_id)


async def _answer_question(body: Question, request_id: str) -> dict:
    store.get_or_create(request_id, body.question)
    span_id = uuid.uuid4().hex[:8]
    trace_id = current_trace_id()  # the OpenTelemetry trace id of this request (lets the UI link to Grafana)
    if trace_id:
        store.register_trace(trace_id, request_id)  # lets the store attribute this trace's spans to this request
    started = time.perf_counter()
    logger.info("support request received", extra={"request_id": request_id, "question_chars": len(body.question)})

    store.add(make_event(request_id, "gateway", "step_start",
                         {"question": body.question, "summary": body.question, "trace_id": trace_id},
                         caller="user", span_id=span_id))
    try:
        resp = await app.state.http.post(f"{AGENTS_URL}/ask", json={"question": body.question},
                                         headers={REQUEST_ID_HEADER: request_id})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        logger.exception("agents service failed", extra={"request_id": request_id, "duration_ms": round(elapsed)})
        store.add(make_event(request_id, "gateway", "step_error", {"error": repr(exc)},
                             duration_ms=elapsed, span_id=span_id))
        store.add(make_event(request_id, "gateway", "request_error", {"error": repr(exc)}))
        raise HTTPException(status_code=502, detail="Agents service failed") from exc

    result = resp.json()
    store.get_or_create(request_id).run_id = result["run_id"]  # needed later to attach user feedback
    elapsed = (time.perf_counter() - started) * 1000
    logger.info("support request finished", extra={"request_id": request_id, "duration_ms": round(elapsed), "agent_steps": result["steps"]})
    store.add(make_event(request_id, "gateway", "step_end", {"summary": "answer returned"},
                         duration_ms=elapsed, span_id=span_id))
    store.add(make_event(request_id, "gateway", "request_done",
                         {"answer": result["answer"], "agent_steps": result["steps"], "trace_id": trace_id,
                          "langsmith_run_id": result["run_id"], "prompt_version": result["prompt_version"],
                          "visited": result["visited"], "tools_used": result["tools_used"],
                          "llm_calls": result["llm_calls"], "input_tokens": result["input_tokens"],
                          "output_tokens": result["output_tokens"], "cost_usd": result["cost_usd"]}, duration_ms=elapsed))
    return {"request_id": request_id, "trace_id": trace_id, **result}


class Feedback(BaseModel):
    request_id: str = Field(pattern=r"^[a-z0-9-]{4,40}$")
    score: int = Field(ge=0, le=1)  # 1 = thumbs up, 0 = thumbs down
    comment: str | None = Field(default=None, max_length=500)


@app.post("/feedback")
async def feedback(body: Feedback) -> dict:
    """A user's thumbs up/down on an answer. Forwarded to the agents service, which attaches it to the LangSmith run."""
    record = store.get(body.request_id)
    if record is None or record.run_id is None:
        raise HTTPException(status_code=404, detail="No answered request with that id")
    try:
        resp = await app.state.http.post(f"{AGENTS_URL}/feedback", timeout=20,
                                         json={"run_id": record.run_id, "score": body.score, "comment": body.comment},
                                         headers={REQUEST_ID_HEADER: body.request_id})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("could not forward feedback", extra={"request_id": body.request_id})
        raise HTTPException(status_code=502, detail="Agents service failed") from exc
    result = resp.json()
    store.add(make_event(body.request_id, "gateway", "feedback", {"score": body.score, "summary": "thumbs up" if body.score else "thumbs down", **result}))
    logger.info("feedback received", extra={"request_id": body.request_id, "score": body.score})
    return result


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


@app.get("/api/logs/{trace_id}")
async def logs_for_trace(trace_id: str = PathParam(pattern=r"^[0-9a-f]{32}$")) -> dict:
    """Every log line (from every service) written while handling one trace, fetched from Loki.

    The X-ray page shows these in the detail drawer. If Loki is not running we answer "unavailable" instead of failing.
    """
    now = time.time_ns()
    try:
        resp = await app.state.http.get(
            f"{LOKI_URL}/loki/api/v1/query_range", timeout=10,
            params={"query": f'{{service_name=~".+"}} | trace_id="{trace_id}"', "limit": 500, "direction": "forward",
                    "start": now - 3600 * 10**9, "end": now + 60 * 10**9},
            headers={"X-Loki-Response-Encoding-Flags": "categorize-labels"})  # asks Loki to include structured metadata
        resp.raise_for_status()
    except httpx.HTTPError:
        return {"available": False, "logs": []}
    logs = []
    for stream in resp.json()["data"]["result"]:
        for value in stream["values"]:
            meta = value[2].get("structuredMetadata", {}) if len(value) > 2 and isinstance(value[2], dict) else {}
            logs.append({"ts": int(value[0]) / 1e9, "service": stream["stream"].get("service_name"),
                         "level": meta.get("detected_level") or stream["stream"].get("detected_level") or "info",
                         "message": value[1], "span_id": meta.get("span_id"),
                         "fields": {k: v for k, v in meta.items() if k not in _HIDDEN_LOG_FIELDS}})
    logs.sort(key=lambda entry: entry["ts"])
    return {"available": True, "logs": logs}


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
