"""Which Gemini model the agents use. Kept in its own tiny module so graph.py and callbacks.py can both import it."""
import os

DEFAULT_MODEL = "gemini-4-argon"


def current_model_name() -> str:
    """The model named by GEMINI_MODEL in .env, or the default."""
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
