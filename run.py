#!/usr/bin/env python3
"""One command to start ShopSense, one command to stop it and leave nothing behind.

    python run.py up          # build + start everything (4 Python services + the observability stack)
    python run.py down        # stop everything and remove containers, network and data volumes
    python run.py status      # what is running, and on which ports

On macOS / Linux type `python3` instead of `python`. Only Docker is required: no virtual environment,
no pip install. This file uses nothing but Python's standard library.

Options:
    up   --no-open      do not open the browser when ready
         --timeout N    seconds to wait for everything to become healthy (default 300)
    down --keep-data    keep the Docker volumes (Prometheus / Tempo / Loki / Grafana data)
         --images       also delete the image built for the services (the next `up` rebuilds it)

Everything here is plain `docker compose --profile app ...`; this script only adds friendly checks
(is Docker running? are the ports free?) and verifies after `down` that nothing is left over.
It only ever touches the Docker project named "shopsense" and never stops anything else.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = "shopsense"                       # must match `name:` in docker-compose.yml
APP_IMAGE = "shopsense-app:local"           # must match `image:` in docker-compose.yml
COMPOSE = ["docker", "compose", "--profile", "app"]
PROJECT_LABEL = f"com.docker.compose.project={PROJECT}"


# ---------------------------------------------------------------------------------------- small helpers
def say(message: str = "") -> None:
    print(message, flush=True)


def run(args: list[str], *, capture: bool = False, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command (no shell) from the project folder. `capture` returns its output instead of printing it."""
    return subprocess.run(args, cwd=ROOT, check=check, text=True, encoding="utf-8", errors="replace",
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is already listening on host:port (we simply try to connect)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def published_ports(config: dict) -> dict[int, str]:
    """{host port: service name} for every port the compose file publishes, taken from `docker compose config --format json`."""
    ports: dict[int, str] = {}
    for name, service in (config.get("services") or {}).items():
        for entry in service.get("ports") or []:
            published = entry.get("published")
            if published:
                ports[int(published)] = name
    return ports


def host_ports_in_ps(ps_output: str) -> set[int]:
    """Host ports out of `docker ps --format {{.Ports}}` text such as '127.0.0.1:8000->8000/tcp, ...'."""
    return {int(match) for match in re.findall(r":(\d+)->", ps_output)}


def project_container_ids() -> list[str]:
    result = run(["docker", "ps", "-a", "-q", "--filter", f"label={PROJECT_LABEL}"], capture=True)
    return result.stdout.split()


def project_ports_in_use_by_us() -> set[int]:
    result = run(["docker", "ps", "--filter", f"label={PROJECT_LABEL}", "--format", "{{.Ports}}"], capture=True)
    return host_ports_in_ps(result.stdout)


def resolved_ports() -> dict[int, str]:
    """The host ports we are about to use, after .env / environment overrides (GATEWAY_PORT, GRAFANA_PORT...)."""
    result = run(COMPOSE + ["config", "--format", "json"], capture=True)
    if result.returncode != 0:
        say(result.stderr.strip())
        raise SystemExit("docker-compose.yml could not be read (see the message above).")
    return published_ports(json.loads(result.stdout))


def gateway_url(ports: dict[int, str]) -> str:
    port = next((p for p, name in ports.items() if name == "gateway"), 8000)
    return f"http://127.0.0.1:{port}"


def grafana_url(ports: dict[int, str]) -> str:
    port = next((p for p, name in ports.items() if name == "grafana"), 3000)
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------------------- preflight
def check_docker() -> None:
    if shutil.which("docker") is None:
        raise SystemExit("Docker was not found. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), "
                         "start it, then run this again.")
    if run(["docker", "info"], capture=True).returncode != 0:
        raise SystemExit("Docker is installed but not running. Start Docker Desktop, wait until it says it is running, "
                         "then run this again.")
    if run(["docker", "compose", "version"], capture=True).returncode != 0:
        raise SystemExit("`docker compose` (v2) is missing. Update Docker Desktop to a recent version.")


def check_ports(ports: dict[int, str]) -> None:
    ours = project_ports_in_use_by_us()           # ports held by an already-running ShopSense are fine
    busy = {port: name for port, name in sorted(ports.items()) if port not in ours and port_in_use(port)}
    if not busy:
        return
    say("These ports are already in use by something else on this computer:")
    for port, name in busy.items():
        say(f"  {port}  (needed by {name})")
    say("")
    say("Close whatever is using them (for example the old run_all.ps1 service windows), or pick other ports:")
    say("  macOS / Linux:   GRAFANA_PORT=3300 python3 run.py up")
    say("  Windows (PowerShell):   $env:GRAFANA_PORT=3300; python run.py up")
    say("Overridable: GATEWAY_PORT, ORDERS_PORT, INVENTORY_PORT, AGENTS_PORT, GRAFANA_PORT.")
    raise SystemExit(1)


def wait_for_http(url: str, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------------------- commands
def cmd_up(args: argparse.Namespace) -> int:
    check_docker()
    ports = resolved_ports()
    check_ports(ports)

    say("Building and starting ShopSense (the first run downloads images and takes a few minutes)...")
    result = run(COMPOSE + ["up", "--build", "-d", "--wait", "--wait-timeout", str(args.timeout)])
    if result.returncode != 0:
        say("")
        say("Something did not start. To see why:   docker compose --profile app logs --tail 50")
        say("To clean up what was started:           python run.py down")
        return 1

    url = gateway_url(ports)
    if not wait_for_http(f"{url}/health", 60):
        say(f"The containers are up but {url}/health did not answer. Try: docker compose --profile app logs gateway")
        return 1

    say("")
    say("ShopSense is running.")
    say(f"  X-ray page (start here): {url}/")
    say(f"  Grafana:                 {grafana_url(ports)}/")
    say("")
    say("When you are done:  python run.py down")
    if not args.no_open:
        webbrowser.open(f"{url}/")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    check_docker()
    ports = resolved_ports()

    say("Stopping ShopSense and removing its containers" + ("..." if args.keep_data else ", network and data volumes..."))
    down = COMPOSE + ["down", "--remove-orphans"] + ([] if args.keep_data else ["--volumes"])
    if run(down).returncode != 0:
        say("`docker compose down` reported a problem (see above). Checking what is left...")

    if args.images:
        # Remove exactly the image this project built (never a wildcard, never the downloaded infra images).
        if run(["docker", "image", "inspect", APP_IMAGE], capture=True).returncode == 0:
            run(["docker", "image", "rm", APP_IMAGE], capture=True)

    # Verify instead of assuming: nothing of ours may remain.
    leftovers = []
    containers = project_container_ids()
    if containers:
        leftovers.append(f"{len(containers)} container(s) still exist")
    if not args.keep_data:
        volumes = run(["docker", "volume", "ls", "-q", "--filter", f"label={PROJECT_LABEL}"], capture=True).stdout.split()
        if volumes:
            leftovers.append(f"volume(s): {', '.join(volumes)}")
    networks = run(["docker", "network", "ls", "-q", "--filter", f"label={PROJECT_LABEL}"], capture=True).stdout.split()
    if networks:
        leftovers.append(f"{len(networks)} network(s) still exist")
    time.sleep(1)  # let the OS release the sockets
    still_busy = [str(port) for port in sorted(ports) if port_in_use(port)]
    if still_busy:
        leftovers.append(f"port(s) still in use: {', '.join(still_busy)} (used by something that is not this project)")

    if leftovers:
        say("")
        say("Not completely clean:")
        for item in leftovers:
            say(f"  - {item}")
        return 1
    say("")
    say("Clean: no ShopSense containers, networks" + ("" if args.keep_data else " or volumes") + " remain, and all its ports are free.")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    check_docker()
    ports = resolved_ports()
    run(COMPOSE + ["ps", "--all"])
    say("")
    say("Ports:")
    for port, name in sorted(ports.items()):
        say(f"  {port:<6} {name:<16} {'listening' if port_in_use(port) else 'free'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start / stop / inspect ShopSense.")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="build and start everything")
    up.add_argument("--no-open", action="store_true", help="do not open the browser")
    up.add_argument("--timeout", type=int, default=300, help="seconds to wait for healthy containers")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="stop everything and clean up")
    down.add_argument("--keep-data", action="store_true", help="keep the Docker volumes")
    down.add_argument("--images", action="store_true", help="also remove the image built for the services")
    down.set_defaults(func=cmd_down)

    status = sub.add_parser("status", help="show containers and ports")
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
