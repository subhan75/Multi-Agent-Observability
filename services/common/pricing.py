"""Turn token counts into an ESTIMATED cost in US dollars.

Prices live in model_prices.json (not in code) because they change. Estimates only: Google's invoice is
the truth. A model that is not in the file costs 0 and a warning is logged once, so a new model never
crashes anything; it just shows up as "free" until you add its price.
"""
import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

PRICES_FILE = Path(__file__).with_name("model_prices.json")
_prices: dict = {key: value for key, value in json.loads(PRICES_FILE.read_text(encoding="utf-8")).items() if not key.startswith("_")}
_warned: set[str] = set()


def price_per_million(model: str, on: date | None = None) -> tuple[float, float] | None:
    """(input_price, output_price) per million tokens in effect on a date, or None if the model is unknown."""
    periods = _prices.get(model)
    if not periods:
        return None
    today = (on or date.today()).isoformat()
    current = [p for p in periods if p["from"] <= today]
    chosen = max(current or periods[:1], key=lambda p: p["from"])
    return chosen["input"], chosen["output"]


def cost_usd(model: str, input_tokens: int, output_tokens: int, on: date | None = None) -> float:
    prices = price_per_million(model, on)
    if prices is None:
        if model not in _warned:
            _warned.add(model)
            logger.warning("no price known for model, cost will be counted as 0", extra={"model": model})
        return 0.0
    price_in, price_out = prices
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
