"""In-memory event store for the gateway: keeps the last N requests and fans events out live."""
import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

MAX_REQUESTS = 50
KEEPALIVE_SECONDS = 15
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


class EventStore:
    """Not thread-safe by design: every method is called on the event loop."""

    def __init__(self) -> None:
        self._records: OrderedDict[str, RequestRecord] = OrderedDict()

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
