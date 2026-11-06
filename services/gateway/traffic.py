"""Synthetic traffic: one button that gives the dashboards something to show.

A single question makes one trace, but a dashboard needs MANY requests before rates, percentiles and error
ratios mean anything. This sends a batch of mock-model questions (so it costs nothing) plus a few direct
order/stock lookups, including some that deliberately fail with 404, so the error panels have data too.

Safety limits: mock model only, at most MAX_COUNT questions, at most CONCURRENCY at the same time, and only one
batch at a time. These requests are not recorded as X-ray requests, so they never push your own requests out of
the "Past requests" list.
"""
import asyncio
import random
from collections.abc import Awaitable, Callable

MAX_COUNT = 20
DEFAULT_COUNT = 12
CONCURRENCY = 3
DIRECT_CALLS_PER_QUESTION = 3

QUESTIONS = [
    "Where is order 42, and can I return it?",
    "Is the product in order 43 in stock?",
    "How long does express shipping take?",
    "Where is order 999?",                      # no such order: a normal "not found", not a failure
    "Has order 44 arrived yet?",
    "Can I cancel order 43?",
    "What is your refund policy?",
    "What is the weather like today?",          # off topic: goes straight to the writer
]

# (service, path, weight): mostly happy paths, some 404s, so the error-ratio panels have something to show.
DIRECT_CALLS = [
    ("orders", "/orders/42", 5), ("orders", "/orders/43", 3), ("orders", "/orders/44", 3), ("orders", "/orders/999", 2),
    ("inventory", "/stock/sku-pencil", 3), ("inventory", "/stock/sku-nope", 1),
]


class TrafficRunner:
    """Runs one batch at a time and reports progress. Not thread-safe: use it only from the event loop."""

    def __init__(self) -> None:
        self.running = False
        self.total = 0
        self.done = 0
        self.failed = 0
        self._task: asyncio.Task | None = None   # keep a reference, or Python may garbage-collect a running task

    def status(self) -> dict:
        return {"running": self.running, "total": self.total, "done": self.done, "failed": self.failed}

    def start(self, count: int, ask: Callable[[str], Awaitable], direct: Callable[[str, str], Awaitable]) -> bool:
        """Begin a batch. Returns False (and does nothing) if one is already running."""
        if self.running:
            return False
        self.running, self.total, self.done, self.failed = True, count, 0, 0
        self._task = asyncio.create_task(self._run(count, ask, direct))
        return True

    async def _run(self, count: int, ask: Callable[[str], Awaitable], direct: Callable[[str, str], Awaitable]) -> None:
        gate = asyncio.Semaphore(CONCURRENCY)
        services, paths, weights = zip(*[(s, p, w) for s, p, w in DIRECT_CALLS])

        async def one(index: int) -> None:
            async with gate:
                try:
                    await ask(QUESTIONS[index % len(QUESTIONS)])
                    for choice in random.choices(range(len(paths)), weights, k=DIRECT_CALLS_PER_QUESTION):
                        await direct(services[choice], paths[choice])
                except Exception:  # noqa: BLE001 - one failed question must not stop the batch
                    self.failed += 1
                finally:
                    self.done += 1

        try:
            await asyncio.gather(*(one(i) for i in range(count)))
        finally:
            self.running = False
