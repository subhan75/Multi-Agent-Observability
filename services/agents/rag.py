"""Minimal retrieval over the policy docs: split by heading, rank by shared words.

Deliberately simple (no embeddings) so Phase 0 stays focused; the retrieval step still
becomes a visible span/run in later phases.
"""
import re
from pathlib import Path

KB_FILE = Path(__file__).parent / "kb" / "policies.md"


def _sections() -> list[str]:
    text = KB_FILE.read_text(encoding="utf-8")
    return [s.strip() for s in re.split(r"(?m)^(?=# )", text) if s.strip()]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def retrieve(question: str, top_k: int = 2) -> list[str]:
    """Return the top_k policy sections sharing the most words with the question."""
    q = _words(question)
    ranked = sorted(_sections(), key=lambda s: len(q & _words(s)), reverse=True)
    return ranked[:top_k]
