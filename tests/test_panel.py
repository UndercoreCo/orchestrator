"""Separate read-only panel, browser session boundary and bounded event tail."""

import json
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_agent_gateway import pilot
from orchestrator.domain.inventory import NodeSettings
from orchestrator.infrastructure.sqlite.panel import PanelJournal
from orchestrator.infrastructure.events import EventTail, MAX_BYTES
from orchestrator.infrastructure.observability import Observability
from orchestrator.application.telemetry import trace_scope
from orchestrator.interfaces.http.panel import create_panel_app
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.bootstrap import panel_app

ORIGIN = "http://127.0.0.1:8793"
TOKEN = "p" * 48
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def panel(pilot):
    gateway, engine, state, body = pilot
    node = gateway.nodes["lab"]
    gateway.store.inventory.seed(
        [
            NodeSettings(
                id=node.id,
                api_url=node.api_url,
                server_id=node.server_id,
                region=node.region,
                capacity=node.capacity,
                mode=node.mode,
                api_key_file="/private/SYNTHETIC-SECRET.token",
            )
        ]
    )
    tail = EventTail(gateway.store.path.parent)
    gateway.telemetry = Observability(event_tail=tail)
    with trace_scope(gateway.telemetry):
        gateway.create(body)
    app = create_panel_app(PanelJournal(gateway.store.path.parent), tail, TOKEN, ORIGIN)
    return TestClient(app, base_url=ORIGIN, raise_server_exceptions=False), gateway, engine, tail


def event(**extra):
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "operation_finished",
        "stage": "create",
        "reason": "ok",
        "action": "create",
        "duration_ms": 12.4,
        "request_id": "a" * 32,
        **extra,
    }


def test_projection_is_local_readonly_and_does_not_leak_keys(panel, monkeypatch):
    client, gateway, engine, tail = panel

    def forbid(*args, **kwargs):
        pytest.fail("Panel contacted node API")

    monkeypatch.setattr(gateway.resources, "request", forbid)
    before = hashlib.sha256(gateway.store.path.read_bytes()).hexdigest()
    result = client.get("/admin/v1/overview", headers=HEADERS)
    assert result.status_code == 200
    data = result.json()
    assert data["nodes_total"] == data["connections_total"] == 1
    assert data["connections"][0]["state"] == "assigned"
    assert data["events_available"] and data["events"]
    for secret in (
        "PrivateKey",
        "api_key",
        "api_url",
        "server_id",
        "creation_expires_at",
        "payload",
        "binding_key",
        "SYNTHETIC-SECRET",
        TOKEN,
        "n" * 40,
    ):
        assert secret not in result.text
    assert hashlib.sha256(gateway.store.path.read_bytes()).hexdigest() == before
    assert "no-store" in result.headers["cache-control"]


def test_backend_and_panel_tokens_cannot_cross_boundaries(panel):
    client, gateway, _, _ = panel
    assert client.get("/admin/v1/overview").status_code == 401
    assert (
        client.get(
            "/admin/v1/overview", headers={"Authorization": "Bearer " + "b" * 40}
        ).status_code
        == 401
    )
    backend = TestClient(create_agent_app(gateway, "b" * 40))
    assert backend.get("/v1/health", headers=HEADERS).status_code == 401
    assert (
        backend.get(
            "/internal/admin/overview", headers={"Authorization": "Bearer " + "b" * 40}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/internal/v2/connections", json={}, headers={"Origin": ORIGIN, **HEADERS}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/admin/v1/overview", json={}, headers={"Origin": ORIGIN, **HEADERS}
        ).status_code
        == 405
    )


def test_session_cookie_logout_and_expiry(panel, monkeypatch):
    client, _, _, _ = panel
    assert client.post("/session", json={"token": TOKEN}).status_code == 403
    result = client.post("/session", json={"token": TOKEN}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    cookie = result.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and TOKEN not in cookie
    assert client.get("/admin/v1/overview").status_code == 200
    assert (
        client.post("/admin/v1/logout", headers={"Origin": "https://foreign.test"}).status_code
        == 403
    )
    assert client.post("/admin/v1/logout", headers={"Origin": ORIGIN}).status_code == 200
    assert client.get("/admin/v1/overview").status_code == 401
    client.post("/session", json={"token": TOKEN}, headers={"Origin": ORIGIN})
    from orchestrator.interfaces.http import panel as module

    original = time.monotonic
    monkeypatch.setattr(module.time, "monotonic", lambda: original() + 3601)
    assert client.get("/admin/v1/overview").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{"Host": "attacker.test:8793"}, {"Sec-Fetch-Site": "cross-site"}, {"Host": "localhost:8793"}],
)
def test_dns_rebinding_and_cross_site_requests_rejected(panel, headers):
    client, _, _, _ = panel
    assert client.get("/admin/v1/overview", headers={**HEADERS, **headers}).status_code == 403


def test_rate_and_request_limits(panel):
    client, _, _, _ = panel
    assert (
        client.post("/session", content=b"x" * 1025, headers={"Origin": ORIGIN}).status_code == 413
    )
    for _ in range(19):
        assert (
            client.post("/session", json={"token": "wrong"}, headers={"Origin": ORIGIN}).status_code
            == 401
        )
    assert (
        client.post("/session", json={"token": TOKEN}, headers={"Origin": ORIGIN}).status_code
        == 429
    )
    assert client.get("/admin/v1/overview?connection_offset=-1", headers=HEADERS).status_code == 422
    assert client.get("/admin/v1/overview?node_offset=1000001", headers=HEADERS).status_code == 422


def test_static_assets_security_headers_and_no_external_dependencies(panel):
    client, _, _, _ = panel
    for path in ["/", "/assets/panel.js", "/assets/commands.js", "/assets/panel.css"]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert "no-store" in response.headers["cache-control"]
        assert TOKEN not in response.text
    assert 'src="/assets/commands.js"' in client.get("/").text
    script = client.get("/assets/panel.js").text
    assert "innerHTML" not in script and "localStorage" not in script
    assert client.get("/assets/unknown").status_code == 404


def test_event_tail_rotation_invalid_lines_and_reader_failures(tmp_path, monkeypatch):
    tail = EventTail(tmp_path)
    for _ in range(3):
        tail.append(event())
    assert len(tail.recent()[0]) == 3
    from orchestrator.infrastructure import events

    monkeypatch.setattr(events, "MAX_BYTES", 1024)
    for _ in range(20):
        tail.append(event())
    assert all(p.stat().st_size <= 1024 for p in tmp_path.glob("*.jsonl"))
    assert 0 < len(tail.recent()[0]) < 20
    tail.append(event(configuration="SECRET"))
    tail.append(event(reason="SECRET"))
    tail.append(event(request_id="SECRET"))
    assert "SECRET" not in "".join(p.read_text() for p in tmp_path.glob("*.jsonl"))
    with (tmp_path / "events.jsonl").open("a") as stream:
        stream.write("not JSON\n")
    assert tail.recent()[1]
    (tmp_path / "events.jsonl").chmod(0o644)
    assert not tail.recent()[1]


def test_empty_and_missing_event_tail_do_not_break_summary(panel):
    client, _, _, tail = panel
    (tail.directory / "events.jsonl").unlink()
    (tail.directory / "events.lock").unlink()
    result = client.get("/admin/v1/overview", headers=HEADERS)
    assert result.status_code == 200 and not result.json()["events_available"]


def test_denied_and_pagination(panel):
    client, gateway, _, _ = panel
    with gateway.store.db() as db:
        for i in range(61):
            db.execute(
                "INSERT INTO assignments(external_id,client_id,node_id,server_id,creation_expires_at,created_at,device_id,binding_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"device-{i}",
                    f"connection-{i}",
                    "lab",
                    "lab-identity",
                    "2099-01-01T00:00:00.000Z",
                    i,
                    f"device-{i}",
                    f"device-{i}",
                ),
            )
    row = gateway.store.get(client_id="connection-0")
    gateway.store.leases.deny(row, True)
    first = client.get("/admin/v1/overview", headers=HEADERS).json()
    second = client.get("/admin/v1/overview?connection_offset=50", headers=HEADERS).json()
    assert len(first["connections"]) == 50 and len(second["connections"]) == 12
    assert first["pending_creates"] == 61 and first["denied"] == 1
    assert set(c["connection_id"] for c in first["connections"]).isdisjoint(
        c["connection_id"] for c in second["connections"]
    )
    assert (
        next(c for c in second["connections"] if c["connection_id"] == "connection-0")["state"]
        == "denied"
    )


def test_bootstrap_refuses_shared_backend_key(tmp_path, monkeypatch):
    from orchestrator.interfaces.cli.initialize import initialize

    result = initialize(tmp_path / "config")
    monkeypatch.setenv("ORCHESTRATOR_SETTINGS", result["settings"])
    settings = json.loads(Path(result["settings"]).read_text())
    with pytest.raises(ValueError, match="must differ"):
        panel_app(settings["backend_token_file"])


def test_missing_database_does_not_create_or_migrate_state(tmp_path):
    app = create_panel_app(PanelJournal(tmp_path), EventTail(tmp_path), TOKEN, ORIGIN)
    client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
    result = client.get("/admin/v1/overview", headers=HEADERS)
    assert result.status_code == 503 and result.json() == {"detail": "panel_unavailable"}
    assert not list(tmp_path.iterdir())


def test_unknown_schema_is_not_migrated(panel):
    client, gateway, _, _ = panel
    with gateway.store.db() as db:
        db.execute("PRAGMA user_version=999")
    before = gateway.store.path.read_bytes()
    response = client.get("/admin/v1/overview", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "panel_unavailable"}
    assert gateway.store.path.read_bytes() == before


def test_panel_reads_schema_two_without_migration(panel):
    from storage_scenarios import promote_schema_two

    client, gateway, _, _ = panel
    promote_schema_two(gateway.store)
    before = gateway.store.path.read_bytes()
    response = client.get("/admin/v1/overview", headers=HEADERS)
    assert response.status_code == 200
    assert gateway.store.path.read_bytes() == before


def test_busy_event_reader_drops_new_event_without_waiting(tmp_path):
    import fcntl

    tail = EventTail(tmp_path)
    tail.append(event())
    with (tmp_path / "events.lock").open("rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        tail.append(event())
    assert len(tail.recent()[0]) == 1
    tail.append(event())
    assert len(tail.recent()[0]) == 2
