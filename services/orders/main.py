"""Orders service: order lookup, enriched with stock info (port 8001)."""
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.common.config import service_url
from services.common.db import connect
from services.common.events import current_headers, emit_event, install_event_middleware
from services.common.metrics import install_metrics
from services.common.telemetry import instrument_app, setup_telemetry

logger = logging.getLogger(__name__)

DB = "orders.db"
INVENTORY_URL = service_url("INVENTORY_URL", 8002)
SEED = [
    (42, "alice", "sku-pencil", 3, "shipped", "2026-10-01"),
    (43, "bob", "sku-notebook", 1, "backordered", "2026-10-10"),
    (44, "carol", "sku-eraser", 2, "delivered", "2026-09-20"),
]


class Order(BaseModel):
    id: int
    customer: str
    sku: str
    quantity: int
    status: str
    placed_on: str
    in_stock_now: int | None = None  # filled in from inventory-service


@asynccontextmanager
async def lifespan(app: FastAPI):
    with connect(DB) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer TEXT, sku TEXT,"
            " quantity INTEGER, status TEXT, placed_on TEXT)"
        )
        conn.executemany("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?)", SEED)
    # One long-lived client: building a new one per request re-loads the TLS certificate bundle
    # (~300 ms on Windows) and opens a fresh connection every time. Found via the latency metrics.
    app.state.http = httpx.AsyncClient(timeout=5)
    yield
    await app.state.http.aclose()


app = FastAPI(title="orders-service", lifespan=lifespan)
install_event_middleware(app, component="orders", caller="order_agent")
install_metrics(app)
setup_telemetry("orders")
instrument_app(app)


@app.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: int) -> Order:
    with connect(DB) as conn:
        # conn.cursor().execute (not conn.execute): OpenTelemetry only traces queries made through a cursor
        row = conn.cursor().execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    emit_event("sqlite", "db_read", {"summary": f"orders[{order_id}] → {dict(row) if row else 'no row'}"}, caller="orders")
    if row is None:
        logger.warning("order not found", extra={"order_id": order_id})
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    order = Order(**row)
    logger.info("order found", extra={"order_id": order_id, "order_status": order.status, "sku": order.sku})

    # Service-to-service call: this hop is what distributed tracing will link up later.
    # current_headers() forwards the X-Request-ID so inventory joins the same request.
    try:
        resp = await app.state.http.get(f"{INVENTORY_URL}/stock/{order.sku}", headers=current_headers())
    except httpx.HTTPError:
        # logger.exception records the full stack trace. We still re-raise: orders cannot answer
        # properly without stock info (a known weakness, left as an exercise to degrade gracefully).
        logger.exception("inventory service unreachable", extra={"order_id": order_id, "sku": order.sku})
        raise
    if resp.status_code == 200:
        order.in_stock_now = resp.json()["quantity"]
    else:
        logger.warning("inventory returned an unexpected status", extra={"sku": order.sku, "inventory_status": resp.status_code})
    return order


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
