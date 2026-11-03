"""In-memory event store for the gateway: keeps the last N requests and fans events out live."""
import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

MAX_REQUESTS = 50
KEEPALIVE_SECONDS = 15
MAX_PENDING_TRACES = 50     # traces whose spans arrived before we knew which request they belong to
MAX_PENDING_EVENTS = 300    # per trace
GRACE_SECONDS = 1.5
TERMINAL_KINDS = {"request_done", "request_error"}


@dataclass
class RequestRecord:
    request_id: str
    question: str = ""
    created: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    finished: bool = False
    run_id: str | None = None   # LangSmith run id, filled in when the agents service answers


class EventStore:
    """Not thread-safe by design: every method is called on the event loop."""

    def __init__(self) -> None:
        self._records: OrderedDict[str, RequestRecord] = OrderedDict()
        self._trace_to_request: OrderedDict[str, str] = OrderedDict()   # OpenTelemetry trace id -> our request id
        self._pending: OrderedDict[str, list[dict]] = OrderedDict()     # span events waiting for that mapping

    def get_or_create(self, request_id: str, question: str = "") -> RequestRecord:
        record = self._records.get(request_id)
        if record is None:
            record = self._records[request_id] = RequestRecord(request_id, question)
            while len(self._records) > MAX_REQUESTS:
                self._records.popitem(last=False)  # drop the oldest
        elif question and not record.question:
            record.question = question
        return record

    def get(self, request_id: str) -> RequestRecord | None:
        return self._records.get(request_id)

    def add(self, event: dict) -> None:
        record = self.get_or_create(event["request_id"])
        event["seq"] = len(record.events)
        record.events.append(event)
        if event["kind"] in TERMINAL_KINDS:
            record.finished = True
        for queue in record.subscribers:
            queue.put_nowait(event)

    def register_trace(self, trace_id: str, request_id: str) -> None:
        """Learn which request a trace belongs to, and release any span events that were waiting for it."""
        self._trace_to_request[trace_id] = request_id
        while len(self._trace_to_request) > 200:
            self._trace_to_request.popitem(last=False)
        for event in self._pending.pop(trace_id, []):
            event["request_id"] = request_id
            self.add(event)

    def add_span_event(self, event: dict) -> None:
        """A span_start / span_end event. Downstream services tag it with the request id; the gateway's own
        root span cannot, so it is matched by trace id (or held until register_trace is called)."""
        trace_id = event.get("otel_trace_id")
        request_id = event.get("request_id") or self._trace_to_request.get(trace_id)
        if request_id:
            event["request_id"] = request_id
            self.add(event)
            return
        bucket = self._pending.setdefault(trace_id, [])
        if len(bucket) < MAX_PENDING_EVENTS:
            bucket.append(event)
        while len(self._pending) > MAX_PENDING_TRACES:
            self._pending.popitem(last=False)   # never let unattributed spans (e.g. static files) pile up

    def summaries(self) -> list[dict]:
        return [
            {"request_id": r.request_id, "question": r.question, "created": r.created,
             "finished": r.finished, "events": len(r.events)}
            for r in reversed(self._records.values())  # newest first
        ]

    async def stream(self, request_id: str) -> AsyncIterator[str]:
        """Yield server-sent-event text: past events first, then live ones until the request ends."""
        record = self.get_or_create(request_id)
        queue: asyncio.Queue = asyncio.Queue()
        record.subscribers.add(queue)  # subscribe BEFORE snapshotting so nothing is missed
        try:
            for event in list(record.events):
                yield f"data: {json.dumps(event)}\n\n"
            if record.finished:
                return
            finishing = False
            while True:
                # After the terminal event, wait only briefly: stragglers from other services
                # (their events travel via a background thread) may still be on their way.
                timeout = GRACE_SECONDS if finishing else KEEPALIVE_SECONDS
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if finishing:
                        return
                    yield ": keepalive\n\n"  # SSE comment line; keeps proxies from closing the stream
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                finishing = finishing or event["kind"] in TERMINAL_KINDS
        finally:
            record.subscribers.discard(queue)
