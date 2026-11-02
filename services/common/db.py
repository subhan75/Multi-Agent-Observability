"""Tiny SQLite helpers shared by the orders and inventory services."""
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_DIR.mkdir(exist_ok=True)


@contextmanager
def connect(db_name: str) -> Iterator[sqlite3.Connection]:
    """Open a connection that commits on success and always closes."""
    conn = sqlite3.connect(DATA_DIR / db_name)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
