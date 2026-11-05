"""Offline tests for the pure helpers in run.py (no Docker needed)."""
import socket

import run


def test_published_ports_reads_compose_config():
    config = {"services": {
        "gateway": {"ports": [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 8000, "published": "8000", "protocol": "tcp"}]},
        "grafana": {"ports": [{"target": 3000, "published": "3300"}]},
        "no-ports": {},
    }}
    assert run.published_ports(config) == {8000: "gateway", 3300: "grafana"}


def test_published_ports_ignores_unpublished_entries():
    assert run.published_ports({"services": {"x": {"ports": [{"target": 80}]}}}) == {}


def test_host_ports_in_ps_parses_docker_output():
    text = "127.0.0.1:8000->8000/tcp, 127.0.0.1:9090->9090/tcp\n0.0.0.0:3100->3100/tcp, :::3100->3100/tcp"
    assert run.host_ports_in_ps(text) == {8000, 9090, 3100}


def test_port_in_use_detects_a_listening_socket():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        busy = server.getsockname()[1]
        assert run.port_in_use(busy) is True
    assert run.port_in_use(busy) is False       # closed again


def test_urls_follow_the_resolved_ports():
    ports = {8123: "gateway", 3300: "grafana"}
    assert run.gateway_url(ports) == "http://127.0.0.1:8123"
    assert run.grafana_url(ports) == "http://127.0.0.1:3300"
    assert run.gateway_url({}) == "http://127.0.0.1:8000"        # defaults when nothing is overridden


def test_commands_only_target_the_shopsense_project():
    assert run.COMPOSE[:2] == ["docker", "compose"]
    assert run.PROJECT_LABEL == "com.docker.compose.project=shopsense"
