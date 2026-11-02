"""Tools the agents can call. Plain functions; the @tool decorator exposes them to the LLM."""
import logging
import time

import httpx
from langchain_core.tools import tool

from services.common.config import service_url
from services.common.events import current_headers, emit_event
from services.common.metrics import record_tool_call
from services.common.telemetry import traced

logger = logging.getLogger(__name__)

ORDERS_URL = service_url("ORDERS_URL", 8001)
_http = httpx.Client(timeout=10)  # shared across calls; building a client per call is slow


@tool
@traced("tool.get_order")
def get_order(order_id: int) -> str:
    """Look up an order by its numeric id. Returns status, quantity, sku and current stock."""
    started = time.perf_counter()
    try:
        resp = _http.get(f"{ORDERS_URL}/orders/{order_id}", headers=current_headers())
    except httpx.HTTPError:
        # No response at all (orders is down or timed out): this is the most likely real failure.
        record_tool_call("get_order", "error")
        logger.exception("tool get_order could not reach orders", extra={"order_id": order_id})
        raise
    result = f"No order found with id {order_id}." if resp.status_code == 404 else resp.text
    # A 404 is a normal outcome ("no such order"), not a failure of the tool; only 5xx etc. count as errors.
    outcome = "not_found" if resp.status_code == 404 else ("ok" if resp.is_success else "error")
    record_tool_call("get_order", outcome)
    log = logger.warning if resp.status_code == 404 else logger.info
    log("tool get_order finished", extra={"order_id": order_id, "http_status": resp.status_code})
    emit_event(
        "order_agent", "tool_call",
        {"tool": "get_order", "args": {"order_id": order_id}, "result": result, "summary": f"get_order({order_id})"},
        caller="order_agent", duration_ms=(time.perf_counter() - started) * 1000,
    )
    if resp.status_code != 404:
        resp.raise_for_status()
    return result
