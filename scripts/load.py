"""Traffic generator: gives Prometheus something to measure.

  python scripts/load.py                      # 30 s of fast, direct calls to orders + inventory
  python scripts/load.py --seconds 60 --rps 20
  python scripts/load.py --support 3          # also send 3 real (slow, LLM-backed) questions
"""
import argparse
import asyncio
import random
import time

import httpx

GATEWAY = "http://127.0.0.1:8000"
ORDERS = "http://127.0.0.1:8001"
INVENTORY = "http://127.0.0.1:8002"

# (url, weight). Mostly happy paths, some 404s, so the error-ratio query has something to show.
DIRECT_CALLS = [
    (f"{ORDERS}/orders/42", 5),
    (f"{ORDERS}/orders/43", 3),
    (f"{ORDERS}/orders/44", 3),
    (f"{ORDERS}/orders/999", 1),              # 404: order does not exist
    (f"{INVENTORY}/stock/sku-pencil", 3),
    (f"{INVENTORY}/stock/sku-nope", 1),       # 404: unknown sku
]
QUESTIONS = [
    "Where is order 42, and can I return it?",
    "Is order 43 in stock?",
    "How long does express shipping take?",
]


async def direct_load(seconds: int, rps: float) -> dict[int, int]:
    """Fire roughly `rps` requests per second for `seconds`, at random endpoints."""
    urls = [u for u, _ in DIRECT_CALLS]
    weights = [w for _, w in DIRECT_CALLS]
    codes: dict[int, int] = {}
    deadline = time.monotonic() + seconds

    async with httpx.AsyncClient(timeout=10) as client:
        async def one(url: str) -> None:
            try:
                status = (await client.get(url)).status_code
            except httpx.HTTPError:
                status = 0  # connection failure
            codes[status] = codes.get(status, 0) + 1

        tasks = []
        while time.monotonic() < deadline:
            tasks.append(asyncio.create_task(one(random.choices(urls, weights)[0])))
            await asyncio.sleep(random.expovariate(rps))  # random gaps, like real users
        await asyncio.gather(*tasks)
    return codes


async def support_load(count: int) -> None:
    async with httpx.AsyncClient(timeout=300) as client:
        async def ask(q: str) -> None:
            started = time.monotonic()
            resp = await client.post(f"{GATEWAY}/support", json={"question": q})
            print(f"  support {resp.status_code} in {time.monotonic() - started:.0f}s: {q}")

        await asyncio.gather(*(ask(QUESTIONS[i % len(QUESTIONS)]) for i in range(count)))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--rps", type=float, default=10)
    parser.add_argument("--support", type=int, default=0, help="number of real LLM-backed questions")
    args = parser.parse_args()

    jobs = [direct_load(args.seconds, args.rps)]
    if args.support:
        jobs.append(support_load(args.support))
    results = await asyncio.gather(*jobs)
    print("direct call status codes:", dict(sorted(results[0].items())))


if __name__ == "__main__":
    asyncio.run(main())
