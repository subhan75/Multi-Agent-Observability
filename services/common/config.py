"""Where services find each other."""
import os


def service_url(env_var: str, default_port: int) -> str:
    """Base URL of another service, from an env var or a default.

    'localhost' is rewritten to '127.0.0.1'. On Windows, 'localhost' resolves to IPv6 (::1) first;
    uvicorn only listens on IPv4, so every call first waits for the IPv6 attempt to fail
    (~400 ms each). Found through the Phase 1 latency metrics.
    """
    url = os.getenv(env_var, f"http://127.0.0.1:{default_port}")
    return url.replace("//localhost", "//127.0.0.1")
