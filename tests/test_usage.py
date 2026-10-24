import pytest
from fastapi.testclient import TestClient
from test_agent_gateway import pilot
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.domain.usage import UsageSample
from orchestrator.domain.models import OrchestratorError

HEADERS = {"Authorization": "Bearer " + "b" * 40}


def test_owned_batch_readthrough_does_not_write_journal(pilot, monkeypatch):
    gateway, engine, _, body = pilot
    connection = gateway.create(body)
    before = gateway.store.rows()
    driver = gateway.drivers.for_protocol("amneziawg")
    calls = []
    monkeypatch.setattr(driver, "usage", lambda *args: calls.append(args) or [])
    client = TestClient(create_agent_app(gateway, "b" * 40))
    target = {"connection_id": connection["client_id"], "device_id": body["external_id"]}
    request = {"schema_version": 1, "connections": [target, {**target, "device_id": "foreign"}]}
    assert client.post("/internal/v2/usage", json=request).status_code == 401
    response = client.post("/internal/v2/usage", headers=HEADERS, json=request)
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
    results = response.json()["results"]
    assert results[0]["samples"] == [] and len(results[0]["allocation_id"]) == 64
    assert results[1]["error"] == "not_found" and len(calls) == 1
    assert gateway.store.rows() == before and len(engine.rows()) == 1


def test_legacy_agent_explicitly_unsupported_not_zero(pilot):
    gateway, _, _, body = pilot
    connection = gateway.create(body)
    client = TestClient(create_agent_app(gateway, "b" * 40))
    response = client.post(
        "/internal/v2/usage",
        headers=HEADERS,
        json={
            "schema_version": 1,
            "connections": [
                {"connection_id": connection["client_id"], "device_id": body["external_id"]}
            ],
        },
    )
    assert response.json()["results"][0]["error"] == "usage_unsupported"
    assert "samples" not in response.json()["results"][0]


def test_driver_authenticates_combined_response_without_health_mutation(pilot, monkeypatch):
    gateway, _, _, body = pilot
    connection = gateway.create(body)
    row = gateway.store.get(client_id=connection["client_id"])
    node = gateway.node(row)
    driver = gateway.drivers.for_node(node)

    def read(n, method, path, **kwargs):
        assert kwargs == {"snapshot": True} and path.endswith("/usage")
        return {
            "schema_version": 1,
            "status": "ok",
            "protocol": "amneziawg",
            "server_id": node.server_id,
            "client_id": row["remote_id"],
            "external_id": row["binding_key"],
            "samples": [],
        }

    monkeypatch.setattr(driver.api, "request", read)
    assert driver.usage(node, row["remote_id"], row["binding_key"]) == []
    with pytest.raises(OrchestratorError):
        driver.usage(node, row["remote_id"], "foreign")


from test_node_switch import pair
from orchestrator.application.connections import Connections
from orchestrator.application.usage import Usage
from orchestrator.domain.usage import UsageRequest


def test_switch_keeps_readable_previous_binding_without_counter_storage(pair, monkeypatch):
    gateway, engines, _, payload, ident, req = pair
    before = gateway.store.get(client_id=ident)
    Connections(gateway).switch(ident, req)
    captured = []
    driver = gateway.drivers.for_protocol("amneziawg")
    monkeypatch.setattr(
        driver,
        "usage",
        lambda node, remote, binding: captured.append((node.id, remote, binding)) or [],
    )
    result = Usage(gateway).batch(
        UsageRequest(
            schema_version=1, connections=[{"connection_id": ident, "device_id": "vpn_device"}]
        )
    )["results"][0]
    assert result["node_id"] == "b" and result["previous"][0]["node_id"] == "a"
    assert ("a", before["remote_id"], before["binding_key"]) in captured
    assert result["allocation_id"] != result["previous"][0]["allocation_id"]
    with gateway.store.db() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert not any("usage" in name or "traffic" in name for name in tables)
