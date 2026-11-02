"""Tools the agents can call. Plain functions; the @tool decorator exposes them to the LLM."""
import time

import httpx
from langchain_core.tools import tool

from services.common.config import service_url
from services.common.events import current_headers, emit_event

ORDERS_URL = service_url("ORDERS_URL", 8001)
_http = httpx.Client(timeout=10)  # shared across calls; building a client per call is slow


@tool
def get_order(order_id: int) -> str:
    """Look up an order by its numeric id. Returns status, quantity, sku and current stock."""
    started = time.perf_counter()
    resp = _http.get(f"{ORDERS_URL}/orders/{order_id}", headers=current_headers())
    result = f"No order found with id {order_id}." if resp.status_code == 404 else resp.text
    emit_event(
        "order_agent", "tool_call",
        {"tool": "get_order", "args": {"order_id": order_id}, "result": result, "summary": f"get_order({order_id})"},
        caller="order_agent", duration_ms=(time.perf_counter() - started) * 1000,
    )
    if resp.status_code != 404:
        resp.raise_for_status()
    return result
