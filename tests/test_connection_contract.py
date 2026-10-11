from test_agent_gateway import pilot  # noqa: F401

"""Contract migration and protocol isolation; no sockets or real credentials."""

import json
import sqlite3
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from orchestrator.bootstrap import build_gateway as AgentGateway
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.application.connections import Connections
from orchestrator.domain.contracts import (
    DeviceRequest,
    ConfigurationRequest,
    ConnectionConfiguration,
    TransportConfiguration,
)
from orchestrator.infrastructure.drivers.amneziawg import AmneziaAgentDriver
from orchestrator.application.ports import DriverCapabilities, NodeConnection
from orchestrator.application.drivers import DriverRegistry
from orchestrator.domain.models import Node, Observation, OrchestratorError
import time

HEADERS = {"Authorization": "Bearer " + "b" * 40}
AWG = {"protocol": "amneziawg", "configuration_version": 1}
TT = {"protocol": "trusttunnel", "configuration_version": 1}


def request(body, capabilities=None):
    return {
        "schema_version": 1,
        "device_id": body["external_id"],
        "name": body["name"],
        "expires_at": body["expires_at"],
        "capabilities": capabilities or [AWG],
    }


def config_request(body, capabilities=None):
    return {k: v for k, v in request(body, capabilities).items() if k not in ("name", "expires_at")}


def test_v1_device_adopts_v2_without_new_peer_or_slot(pilot):
    gateway, engine, _, body = pilot
    original = gateway.create(body)
    client = TestClient(create_agent_app(gateway, "b" * 40))
    result = client.post("/internal/v2/connections", headers=HEADERS, json=request(body))
    assert result.status_code == 200
    value = result.json()
    assert value["device_id"] == body["external_id"]
    assert value["connection_id"] == original["client_id"]
    assert len(engine.rows()) == len(gateway.store.for_device(body["external_id"])) == 1
    exported = client.post(
        "/internal/v2/connections/" + value["connection_id"] + "/configuration",
        headers=HEADERS,
        json=config_request(body),
    )
    assert exported.status_code == 200 and "no-store" in exported.headers["cache-control"]
    decoded = ConnectionConfiguration.model_validate(exported.json())
    assert decoded.protocol == "amneziawg" and decoded.configuration.format == "awg-quick"
    assert decoded.configuration.data == gateway.client(original["client_id"], "configuration")
    assert "PrivateKey" not in repr(decoded)


def test_no_compatible_protocol_does_not_reserve_or_create(pilot):
    gateway, engine, _, body = pilot
    service = Connections(gateway)
    for offer in ([TT], [{"protocol": "amneziawg", "configuration_version": 99}]):
        with pytest.raises(OrchestratorError, match="no_compatible_protocol"):
            service.create(DeviceRequest.model_validate(request(body, offer)))
    assert not gateway.store.rows() and not engine.rows()


def test_existing_incompatible_binding_is_not_moved(pilot):
    gateway, engine, _, body = pilot
    gateway.create(body)
    with pytest.raises(OrchestratorError, match="client_upgrade_required"):
        Connections(gateway).create(DeviceRequest.model_validate(request(body, [TT])))
    assert len(gateway.store.rows()) == len(engine.rows()) == 1


def test_lost_response_and_concurrency_survive_v2_restart(pilot):
    gateway, engine, state, body = pilot
    value = DeviceRequest.model_validate(request(body))
    state["lose"] = True
    with pytest.raises(OrchestratorError):
        Connections(gateway).create(value)
    restarted = AgentGateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: Connections(restarted).create(value), range(3)))
    assert len({r.connection_id for r in results}) == len(engine.rows()) == 1


def test_wrong_device_cannot_read_export_or_revoke(pilot):
    gateway, engine, _, body = pilot
    ident = gateway.create(body)["client_id"]
    client = TestClient(create_agent_app(gateway, "b" * 40))
    prefix = "/internal/v2/connections/" + ident
    responses = [
        client.get(prefix, params={"device_id": "other"}, headers=HEADERS),
        client.post(
            prefix + "/configuration",
            headers=HEADERS,
            json={**config_request(body), "device_id": "other"},
        ),
        client.post(prefix + "/disable", headers=HEADERS, json={"device_id": "other"}),
    ]
    assert all(r.status_code == 404 for r in responses)
    assert engine.backend.peers


def test_http_rejects_unsupported_schema_duplicate_offers_and_secret_inputs(pilot):
    gateway, _, _, body = pilot
    client = TestClient(create_agent_app(gateway, "b" * 40))
    data = request(body)
    for override in (
        {"schema_version": 9},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"capabilities": [AWG, AWG]},
        {"capabilities": []},
        {"capabilities": [{"protocol": "other", "configuration_version": 1}]},
        {"password": "synthetic-secret-never-return"},
    ):
        response = client.post(
            "/internal/v2/connections", headers=HEADERS, json={**data, **override}
        )
        assert response.status_code == 422 and "synthetic-secret" not in response.text
        assert "no-store" in response.headers["cache-control"]
    assert client.post("/internal/v2/connections", json=data).status_code == 401
    assert (
        client.post("/internal/v2/connections", headers=HEADERS, content="x" * 8193).status_code
        == 413
    )
    discovery = client.get("/internal/v2/capabilities", headers=HEADERS).json()
    assert [c["protocol"] for c in discovery["protocols"]] == ["amneziawg"]


def test_revision_stable_on_repeat_restart_and_changes_without_storing_keys(pilot):
    gateway, engine, _, body = pilot
    ident = gateway.create(body)["client_id"]
    query = ConfigurationRequest.model_validate(config_request(body))
    first = Connections(gateway).configuration(ident, query)
    restarted = AgentGateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    assert Connections(restarted).configuration(ident, query).revision == first.revision == 1
    engine.backend.config["endpoint"] = "192.0.2.99:8443"
    second = Connections(restarted).configuration(ident, query)
    assert second.revision == 2 and second.configuration.data != first.configuration.data
    private = first.configuration.data.split("PrivateKey = ")[1].splitlines()[0]
    assert private.encode() not in gateway.store.path.read_bytes()
    assert b"[Interface]" not in gateway.store.path.read_bytes()
    # An older in-flight export cannot overwrite a newer published revision.
    old_row = {**gateway.store.get(client_id=ident), "configuration_revision": 1}
    with pytest.raises(OrchestratorError, match="configuration_changed"):
        gateway.store.configuration_revision(old_row, first.configuration, first.expires_at)


def test_confirmed_revoke_blocks_v2_configuration(pilot):
    gateway, engine, _, body = pilot
    ident = gateway.create(body)["client_id"]
    client = TestClient(create_agent_app(gateway, "b" * 40))
    result = client.post(
        f"/internal/v2/connections/{ident}/disable",
        headers=HEADERS,
        json={"device_id": body["external_id"]},
    )
    assert result.status_code == 200 and result.json()["state"] == "disabled"
    assert not engine.backend.peers
    response = client.post(
        f"/internal/v2/connections/{ident}/configuration",
        headers=HEADERS,
        json=config_request(body),
    )
    assert response.status_code == 410


def test_migration_keeps_legacy_alias_remote_identity_and_expiry(tmp_path):
    folder = tmp_path / "state"
    folder.mkdir(mode=0o700)
    path = folder / "assignments.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE assignments (external_id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE, "
            "node_id TEXT NOT NULL, server_id TEXT NOT NULL, creation_expires_at TEXT NOT NULL, "
            "remote_id TEXT, created_at REAL NOT NULL, last_operation TEXT, last_outcome TEXT, checked_at REAL)"
        )
        db.execute(
            "INSERT INTO assignments VALUES ('device','wgapi_alias','lab','identity','2099-01-01T00:00:00.000Z','remote',1,NULL,NULL,NULL)"
        )
    path.chmod(0o600)
    store = Assignments(folder)
    row = store.get(external_id="device")
    assert row["client_id"] == "wgapi_alias" and row["remote_id"] == "remote"
    assert row["device_id"] == "device" and row["protocol"] == "amneziawg"
    assert row["creation_expires_at"] == "2099-01-01T00:00:00.000Z"
    assert Assignments(folder).rows() == store.rows()


class SyntheticTrustTunnelDriver:
    """Contract test double only: not a runtime TrustTunnel implementation."""

    capabilities = DriverCapabilities(
        "trusttunnel", idempotent_create=True, enforces_expiry=True, confirmed_revoke=True
    )

    def __init__(self):
        self.clients = {}

    def observe(self, node):
        return Observation(node, len(self.clients), node.capacity, time.time())

    def list(self, node):
        return list(self.clients.values())

    def create(self, node, grant):
        payload = {
            "external_id": grant.binding_key,
            "name": grant.name,
            "expires_at": grant.expires_at,
        }
        remote_id = "tt-remote-" + payload["external_id"]
        self.clients.setdefault(
            remote_id,
            {
                **payload,
                "client_id": remote_id,
                "status": "active",
                "created_at": payload["expires_at"],
                "updated_at": payload["expires_at"],
            },
        )
        return self.clients[remote_id]

    def get(self, node, remote_id):
        return self.clients[remote_id]

    def mutate(self, node, remote_id, operation, payload):
        self.clients[remote_id]["status"] = "disabled"
        return self.clients[remote_id]

    def validate(self, data, external_id=None, client_id=None):
        assert data["external_id"] == external_id and (
            client_id is None or data["client_id"] == client_id
        )
        return data

    def connection(self, node, remote_id, binding_key):
        return NodeConnection(self.get(node, remote_id), self.configuration(node, remote_id))

    def configuration(self, node, remote_id):
        return TransportConfiguration(
            protocol="trusttunnel",
            format="trusttunnel-toml",
            data='[endpoint]\nusername="synthetic"\npassword="not-a-real-secret"\n',
        )


def test_trusttunnel_test_double_uses_same_contract_without_awg_parsing(pilot):
    gateway, _, _, body = pilot
    tt = SyntheticTrustTunnelDriver()
    node = Node(
        "tt", "https://tt.example.test", "tt-identity", "nl", 5, "t" * 40, protocol="trusttunnel"
    )
    registry = DriverRegistry([AmneziaAgentDriver(gateway.resources), tt])
    gateway = AgentGateway(
        [*gateway.nodes.values(), node], gateway.store, gateway.resources, registry
    )
    service = Connections(gateway)
    result = service.create(DeviceRequest.model_validate(request(body, [TT])))
    exported = service.configuration(
        result.connection_id, ConfigurationRequest.model_validate(config_request(body, [TT]))
    )
    assert exported.configuration.format == "trusttunnel-toml"
    assert "[Interface]" not in exported.configuration.data
    assert exported.device_id == body["external_id"]
    assert "not-a-real-secret" not in repr(exported)
    assert b"not-a-real-secret" not in gateway.store.path.read_bytes()
    client = TestClient(create_agent_app(gateway, "b" * 40))
    assert client.get("/v1/clients/" + result.connection_id, headers=HEADERS).status_code == 404
    assert gateway.listing() == {"clients": []}
    with pytest.raises(OrchestratorError, match="client_upgrade_required"):
        service.configuration(
            result.connection_id, ConfigurationRequest.model_validate(config_request(body))
        )


def test_same_device_can_hold_distinct_protocol_bindings_in_journal(tmp_path):
    store = Assignments(tmp_path / "state")
    awg = Node("awg", "https://awg.test", "awg-id", "nl", 5, "a" * 40)
    tt = replace(awg, id="tt", server_id="tt-id", api_url="https://tt.test", protocol="trusttunnel")
    payload = {"external_id": "binding-awg", "expires_at": "2099-01-01T00:00:00.000Z"}
    store.reserve(
        payload, [Observation(awg, 0, 5, time.time())], device_id="one-slot", protocol="amneziawg"
    )
    store.reserve(
        {**payload, "external_id": "binding-tt"},
        [Observation(tt, 0, 5, time.time())],
        device_id="one-slot",
        protocol="trusttunnel",
    )
    assert len(store.for_device("one-slot")) == 2
    assert len({r["device_id"] for r in store.rows()}) == 1


def test_unsafe_driver_is_not_admitted_and_protocol_identity_is_pinned(pilot):
    gateway, _, _, body = pilot
    driver = SyntheticTrustTunnelDriver()
    driver.capabilities = DriverCapabilities("trusttunnel", confirmed_revoke=False)
    with pytest.raises(ValueError):
        DriverRegistry([driver])
    ident = gateway.create(body)["client_id"]
    gateway.nodes["lab"] = replace(gateway.nodes["lab"], protocol="trusttunnel")
    with pytest.raises(OrchestratorError, match="assigned_node_identity_changed"):
        gateway.client(ident)


def test_protocol_payload_format_mismatch_is_rejected():
    with pytest.raises(ValidationError):
        TransportConfiguration(protocol="trusttunnel", format="awg-quick", data="not-a-real-secret")


def test_checked_in_schema_matches_implementation():
    from orchestrator.domain.contracts import schemas

    path = (
        Path(__file__).resolve().parents[1] / "contracts" / "orchestrator-connections.schema.json"
    )
    assert json.loads(path.read_text()) == schemas()


def test_concurrent_exports_keep_one_revision(pilot):
    gateway, _, _, body = pilot
    ident = gateway.create(body)["client_id"]
    query = ConfigurationRequest.model_validate(config_request(body))
    with ThreadPoolExecutor(max_workers=4) as pool:
        manifests = list(
            pool.map(lambda _: Connections(gateway).configuration(ident, query), range(4))
        )
    assert {m.revision for m in manifests} == {1}


def test_disable_and_expiry_are_still_enforced_after_restart(pilot):
    from datetime import timedelta
    from app.domain import now, stamp

    gateway, engine, _, body = pilot
    ident = gateway.create(body)["client_id"]
    with engine.store.db() as db:
        db.execute("UPDATE clients SET expires_at=?", (stamp(now() - timedelta(seconds=1)),))
    restarted = AgentGateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    with pytest.raises(OrchestratorError, match="access_unavailable"):
        Connections(restarted).configuration(
            ident, ConfigurationRequest.model_validate(config_request(body))
        )
    assert not engine.backend.peers


def test_no_duplicate_device_protocol_binding_is_reserved(tmp_path):
    store = Assignments(tmp_path / "state")
    node = Node("awg", "https://awg.test", "awg-id", "nl", 5, "a" * 40)
    payload = {"external_id": "one", "expires_at": "2099-01-01T00:00:00.000Z"}
    observed = [Observation(node, 0, 5, time.time())]
    first = store.reserve(payload, observed, device_id="same-device")
    with pytest.raises(OrchestratorError, match="assignment_identity_conflict"):
        store.reserve({**payload, "external_id": "different"}, observed, device_id="same-device")
    assert store.rows() == [first]


def test_legacy_runtime_cannot_enable_trusttunnel_by_setting_only(pilot):
    gateway, _, _, _ = pilot
    node = replace(next(iter(gateway.nodes.values())), protocol="trusttunnel")
    with pytest.raises(OrchestratorError, match="unsupported_protocol"):
        AgentGateway([node], gateway.store, gateway.resources)


def test_protocol_payload_limits_and_dates_are_validated():
    for data in ("x\0y", "я" * 40000):
        with pytest.raises(ValidationError):
            TransportConfiguration(protocol="amneziawg", format="awg-quick", data=data)
    with pytest.raises(ValidationError):
        DeviceRequest(
            schema_version=1,
            device_id="device",
            name="Mac",
            capabilities=[AWG],
            expires_at="2099-02-31T00:00:00.000Z",
        )


def test_runtime_does_not_silently_treat_trusttunnel_as_awg(tmp_path, monkeypatch):
    from orchestrator.runtime import agent_app

    token = tmp_path / "token"
    token.write_text("x" * 40)
    token.chmod(0o600)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "state_directory": str(tmp_path / "state"),
                "backend_token_file": str(token),
                "nodes": [
                    {
                        "id": "tt",
                        "server_id": "identity",
                        "api_url": "https://example.test",
                        "region": "nl",
                        "capacity": 5,
                        "mode": "active",
                        "protocol": "trusttunnel",
                        "api_key_file": str(token),
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("ORCHESTRATOR_LAB", "1")
    monkeypatch.setenv("ORCHESTRATOR_SETTINGS", str(settings))
    with pytest.raises(OrchestratorError, match="unsupported_protocol"):
        agent_app()


def test_v2_lookup_is_owned_read_only_and_returns_no_configuration(pilot):
    gateway, engine, _, body = pilot
    service = Connections(gateway)
    assert service.lookup("missing") == {"connections": []}
    assert not gateway.store.rows() and not engine.rows()
    result = service.create(DeviceRequest.model_validate(request(body)))
    found = service.lookup(result.device_id)
    assert found == {"connections": [result.model_dump()]}
    assert service.lookup("other-device") == {"connections": []}
    assert "configuration" not in found["connections"][0]
