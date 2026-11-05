"""Inventory service: stock levels per product (port 8002)."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.common import chaos
from services.common.chaos import install_chaos_middleware
from services.common.db import connect
from services.common.events import emit_event, install_event_middleware
from services.common.metrics import install_metrics
from services.common.telemetry import instrument_app, setup_telemetry

logger = logging.getLogger(__name__)

DB = "inventory.db"
SEED = [("sku-pencil", "Graphite Pencil", 120), ("sku-notebook", "A5 Notebook", 0), ("sku-eraser", "Eraser", 45)]


class StockItem(BaseModel):
    sku: str
    name: str
    quantity: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at startup: create the table and seed it."""
    with connect(DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS stock (sku TEXT PRIMARY KEY, name TEXT, quantity INTEGER)")
        conn.executemany("INSERT OR IGNORE INTO stock VALUES (?, ?, ?)", SEED)
    yield


app = FastAPI(title="inventory-service", lifespan=lifespan)
install_event_middleware(app, component="inventory", caller="orders")
install_chaos_middleware(app)  # optional injected problems (off unless CHAOS_ENABLED=1)
install_metrics(app)
setup_telemetry("inventory")
instrument_app(app)


@app.get("/stock/{sku}", response_model=StockItem)
def get_stock(sku: str) -> StockItem:
    if chaos.active("inventory_down"):   # injected problem (see services/common/chaos.py)
        logger.error("inventory unavailable (injected failure)", extra={"sku": sku})
        raise HTTPException(status_code=503, detail="Inventory is down (injected problem)")
    with connect(DB) as conn:
        # conn.cursor().execute (not conn.execute): OpenTelemetry only traces queries made through a cursor
        row = conn.cursor().execute("SELECT * FROM stock WHERE sku = ?", (sku,)).fetchone()
    emit_event("sqlite", "db_read", {"summary": f"stock[{sku}] → {dict(row) if row else 'no row'}"}, caller="inventory")
    if row is None:
        logger.warning("unknown sku requested", extra={"sku": sku})
        raise HTTPException(status_code=404, detail=f"Unknown sku {sku}")
    return StockItem(**row)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
