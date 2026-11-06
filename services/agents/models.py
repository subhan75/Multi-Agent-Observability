"""Which model the agents use: real Gemini or the built-in mock. Kept in its own tiny module so graph.py and callbacks.py can both import it.

The choice is made PER REQUEST (the "Mock AI / Live Gemini" switch in the X-ray page). It travels in the
`X-LLM-Mode` header from the browser to the gateway to the agents service, which stores it in a ContextVar
(a variable that is separate for each request) so every model call made for that request sees the same choice.
"""
import os
from contextvars import ContextVar

DEFAULT_MODEL = "gemini-4-argon"
MOCK_MODEL_NAME = "mock-llm"
MODES = ("mock", "live")

# "mock" or "live" for the request being handled; None outside a request (evals, scripts) -> default_mode().
llm_mode_var: ContextVar[str | None] = ContextVar("llm_mode", default=None)

# What .env.example ships with; copying that file without editing it must not count as having a key.
_PLACEHOLDER_KEYS = {"", "your-gemini-api-key"}


def live_available() -> bool:
    """True if a real Gemini API key is configured."""
    return os.getenv("GOOGLE_API_KEY", "").strip() not in _PLACEHOLDER_KEYS


def default_mode() -> str:
    """LLM_MODE from the environment if set to mock/live, otherwise live when a key exists, else mock."""
    forced = os.getenv("LLM_MODE", "").strip().lower()
    if forced in MODES:
        return forced
    return "live" if live_available() else "mock"


def resolve_mode(requested: str | None) -> str:
    """The mode asked for by the caller if valid, otherwise the default."""
    requested = (requested or "").strip().lower()
    return requested if requested in MODES else default_mode()


def current_mode() -> str:
    return llm_mode_var.get() or default_mode()


def live_model_name() -> str:
    """The real Gemini model named by GEMINI_MODEL in .env (else the default)."""
    return os.getenv("GEMINI_MODEL") or DEFAULT_MODEL  # `or`: an empty value (as docker-compose passes) also means "use the default"


def current_model_name() -> str:
    """The model in use right now: "mock-llm", or the real Gemini model."""
    return MOCK_MODEL_NAME if current_mode() == "mock" else live_model_name()
