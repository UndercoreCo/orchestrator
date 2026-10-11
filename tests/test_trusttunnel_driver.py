"""Wire-level adapter tests, NOT evidence that a real TrustTunnel agent enforces lifecycle."""

from copy import deepcopy
from dataclasses import replace
import json
import tomllib

import httpx
import pytest
from test_trusttunnel_configuration import document
from orchestrator.application.drivers import DriverRegistry
from orchestrator.application.ports import NodeGrant
from orchestrator.bootstrap import build_gateway
from orchestrator.domain.models import Node, OrchestratorError
from orchestrator.infrastructure.drivers.node_http import NodeHTTP
from orchestrator.infrastructure.drivers.trusttunnel import TrustTunnelAgentDriver
from orchestrator.infrastructure.drivers.trusttunnel_contract import ConnectionSnapshot

REMOTE = "ttapi_" + "a" * 32
OTHER = "ttapi_" + "b" * 32
EXPIRY = "2099-01-01T00:00:00.000Z"
LATER = "2099-02-01T00:00:00.000Z"
CLIENT = dict(
    client_id=REMOTE,
    external_id="binding-example",
    name="Test device",
    expires_at=EXPIRY,
    status="active",
    created_at="2026-01-01T00:00:00.000Z",
    updated_at="2026-01-01T00:00:00.000Z",
)


def envelope(**fields):
    return dict(
        schema_version=1, server_id="trust-lab", protocol="trusttunnel", status="ok", **fields
    )


def snapshot(**changes):
    return envelope(applied=True, client={**CLIENT, **changes})


@pytest.fixture
def wire():
    calls = []
    state = dict(
        health=envelope(
            capabilities=dict(
                idempotent_create=True,
                enforces_expiry=True,
                confirmed_revoke=True,
            )
        ),
        reply=snapshot(),
        status=200,
        cache="private, no-store",
        lose=False,
    )
    node = Node(
        "trust",
        "https://node.example.invalid",
        "trust-lab",
        "test",
        100,
        "x" * 40,
        protocol="trusttunnel",
    )

    def handle(request):
        assert request.headers["authorization"] == "Bearer " + node.api_key
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=state["health"], headers={"cache-control": "no-store"})
        if state["lose"]:
            state["lose"] = False
            raise httpx.ReadTimeout("must-not-leak-upstream-details")
        return httpx.Response(
            state["status"], json=state["reply"], headers={"cache-control": state["cache"]}
        )

    with NodeHTTP(transport=httpx.MockTransport(handle)) as api:
        yield TrustTunnelAgentDriver(api), node, state, calls


def connection(state, **changes):
    state["reply"] = dict(
        snapshot(),
        endpoint_export=document(username=REMOTE),
        ipv6_available=False,
        transport="http2",
    )
    state["reply"].update(changes)


def test_trusttunnel_requires_explicit_bootstrap_opt_in(wire):
    driver, node, _, _ = wire
    driver.verify(node)
    assert DriverRegistry([driver]).for_protocol("trusttunnel") is driver
    from orchestrator.bootstrap import configured_drivers

    assert (
        configured_drivers(driver.api, True)
        .for_protocol("trusttunnel")
        .capabilities.confirmed_revoke
    )
    gateway = build_gateway([], None)
    try:
        with pytest.raises(OrchestratorError, match="unsupported_protocol"):
            gateway.drivers.for_protocol("trusttunnel")
    finally:
        gateway.close()


def test_create_and_retry_preserve_request_after_unknown_result(wire):
    driver, node, state, calls = wire
    grant = NodeGrant(CLIENT["external_id"], CLIENT["name"], EXPIRY)
    state["lose"] = True
    with pytest.raises(OrchestratorError, match="^node_unavailable$"):
        driver.create(node, grant)
    # No automatic retry inside the adapter after a potentially applied mutation.
    assert len([c for c in calls if c[0] == "POST"]) == 1
    first = driver.create(node, grant)
    assert driver.create(node, grant) == first
    posts = [c for c in calls if c[0] == "POST"]
    assert (
        posts
        == [
            (
                "POST",
                "/v1/clients",
                dict(
                    external_id=CLIENT["external_id"],
                    name=CLIENT["name"],
                    expires_at=EXPIRY,
                ),
            )
        ]
        * 3
    )


@pytest.mark.parametrize("transport", ["http2", "http3"])
def test_atomic_connection_validates_identity_and_uses_node_ipv6(wire, transport):
    driver, node, state, calls = wire
    connection(state, transport=transport)
    result = driver.connection(node, REMOTE, CLIENT["external_id"])
    endpoint = tomllib.loads(result.configuration.data)["endpoint"]
    assert result.client == CLIENT
    assert endpoint["has_ipv6"] is False
    assert endpoint["upstream_protocol"] == transport
    assert result.configuration.protocol == "trusttunnel"
    assert result.configuration.format == "trusttunnel-toml"
    assert endpoint["password"] not in repr(result)
    assert endpoint["password"] not in repr(ConnectionSnapshot.model_validate(state["reply"]))
    assert [c[1] for c in calls] == ["/v1/health", f"/v1/clients/{REMOTE}/connection"]
    assert driver.configuration(node, REMOTE) == result.configuration


def test_get_list_and_capacity_observation(wire):
    driver, node, state, _ = wire
    assert driver.get(node, REMOTE) == CLIENT
    state["reply"] = envelope(clients=[CLIENT])
    assert driver.list(node) == [CLIENT]
    result = driver.observe(node)
    assert result.total_peers == 1 and result.max_peers == 100


@pytest.mark.parametrize(
    "operation,status,body",
    [
        ("disable", "disabled", {}),
        ("disable", "expired", {}),
        ("enable", "active", {}),
        ("renew", "active", dict(expires_at=LATER, idempotency_key="renew-example")),
        (
            "replace",
            "active",
            dict(
                expires_at=LATER,
                idempotency_key="rotate-example",
                expected_external_id=CLIENT["external_id"],
                allow_create=False,
            ),
        ),
    ],
)
def test_mutations_preserve_identity_and_operation_keys(wire, operation, status, body):
    driver, node, state, calls = wire
    state["reply"] = snapshot(status=status, expires_at=body.get("expires_at", EXPIRY))
    first = driver.mutate(node, REMOTE, operation, body)
    assert driver.mutate(node, REMOTE, operation, body) == first
    assert first["client_id"] == REMOTE and first["status"] == status
    assert [c for c in calls if c[0] == "POST"] == [
        ("POST", f"/v1/clients/{REMOTE}/{operation}", body)
    ] * 2


@pytest.mark.parametrize(
    "change",
    [
        {"server_id": "wrong-node"},
        {"protocol": "amneziawg"},
        {"schema_version": 2},
        {"schema_version": True},
        {"capabilities": {}},
        {
            "capabilities": dict(
                idempotent_create=True, enforces_expiry=False, confirmed_revoke=True
            )
        },
        {"capabilities": dict(idempotent_create=True, enforces_expiry=True, confirmed_revoke=1)},
    ],
)
def test_health_failure_prevents_mutation(wire, change):
    driver, node, state, calls = wire
    state["health"].update(change)
    with pytest.raises(OrchestratorError):
        driver.mutate(node, REMOTE, "disable", {})
    assert [c[0] for c in calls] == ["GET"]


@pytest.mark.parametrize(
    "change",
    [
        {"server_id": "wrong-node"},
        {"protocol": "amneziawg"},
        {"schema_version": True},
        {"applied": False},
        {"applied": 1},
        {"unexpected": "must-not-leak"},
        {"client": {**CLIENT, "client_id": OTHER}},
        {"client": {**CLIENT, "external_id": "wrong-binding"}},
        {"client": {**CLIENT, "expires_at": "invalid-time"}},
        {"client": {**CLIENT, "status": "unknown"}},
        {"ipv6_available": "false"},
        {"transport": "invalid"},
        {"endpoint_export": document(username=OTHER)},
        {"endpoint_export": document(username=REMOTE, skip_verification=True)},
        {"endpoint_export": "private-unparseable-payload"},
        {"endpoint_export": "x" * 65537},
    ],
)
def test_invalid_snapshot_never_returns_secrets(wire, change):
    driver, node, state, _ = wire
    connection(state, **change)
    with pytest.raises(OrchestratorError) as failure:
        driver.connection(node, REMOTE, CLIENT["external_id"])
    assert failure.value.code in ("node_identity_invalid", "node_response_invalid")
    assert str(failure.value) == failure.value.code
    assert failure.value.__suppress_context__ or failure.value.__context__ is None


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "disabled"},
        {"status": "expired"},
        {"status": "active", "expires_at": "2020-01-01T00:00:00.000Z"},
    ],
)
def test_inactive_or_expired_snapshot_is_not_usable(wire, changes):
    driver, node, state, _ = wire
    connection(state, client={**CLIENT, **changes})
    with pytest.raises(OrchestratorError, match="access_unavailable") as failure:
        driver.connection(node, REMOTE, CLIENT["external_id"])
    assert failure.value.status == 410


@pytest.mark.parametrize(
    "operation,changes",
    [
        ("disable", {"applied": False}),
        ("disable", {"client": CLIENT}),
        ("disable", {"client": {**CLIENT, "client_id": OTHER, "status": "disabled"}}),
        ("enable", {"client": {**CLIENT, "status": "disabled"}}),
        ("renew", {"client": CLIENT}),
    ],
)
def test_unconfirmed_or_wrong_mutation_result_is_failure(wire, operation, changes):
    driver, node, state, _ = wire
    state["reply"].update(changes)
    payload = (
        dict(expires_at=LATER, idempotency_key="repeat-same-key") if operation == "renew" else {}
    )
    with pytest.raises(OrchestratorError):
        driver.mutate(node, REMOTE, operation, payload)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, 503),
        (302, 503),
        (404, 404),
        (405, 503),
        (409, 409),
        (410, 410),
        (422, 422),
        (500, 503),
    ],
)
def test_upstream_errors_are_bounded_no_fallback_or_retry(wire, status, expected):
    driver, node, state, calls = wire
    state.update(status=status, reply={"detail": "upstream-private-material"})
    with pytest.raises(OrchestratorError) as failure:
        driver.connection(node, REMOTE, CLIENT["external_id"])
    assert failure.value.status == expected
    assert "upstream-private" not in str(failure.value)
    assert len(calls) == 2


@pytest.mark.parametrize("cache", ["public, max-age=3600", "x-no-store", 'private="no-store"'])
def test_connection_requires_no_store(wire, cache):
    driver, node, state, _ = wire
    connection(state)
    state["cache"] = cache
    with pytest.raises(OrchestratorError):
        driver.connection(node, REMOTE, CLIENT["external_id"])


@pytest.mark.parametrize("clients", [[CLIENT, CLIENT], [CLIENT, {**CLIENT, "client_id": OTHER}]])
def test_duplicate_list_identity_is_not_capacity(wire, clients):
    driver, node, state, _ = wire
    state["reply"] = envelope(clients=deepcopy(clients))
    with pytest.raises(OrchestratorError):
        driver.observe(node)


@pytest.mark.parametrize("remote", ["../health", REMOTE + "?x=1", "wgapi_" + "a" * 32])
def test_invalid_remote_id_never_reaches_transport(wire, remote):
    driver, node, _, calls = wire
    with pytest.raises(OrchestratorError):
        driver.get(node, remote)
    assert calls == []


@pytest.mark.parametrize("changes", [{"protocol": "amneziawg"}, {"lease_enabled": True}])
def test_wrong_protocol_and_unimplemented_leases_rejected_before_http(wire, changes):
    driver, node, _, calls = wire
    with pytest.raises(OrchestratorError):
        driver.verify(replace(node, **changes))
    assert calls == []


def test_unsupported_exports_and_invalid_mutations_do_not_call_node(wire):
    driver, node, _, calls = wire
    for format in ("conf", "amnezia-vpn", "qr", "toml"):
        with pytest.raises(OrchestratorError, match="unsupported_format"):
            driver.export(node, REMOTE, format)
    for operation, payload in (("renew", {}), ("disable", {"unexpected": True}), ("reboot", {})):
        with pytest.raises(OrchestratorError):
            driver.mutate(node, REMOTE, operation, payload)
    assert calls == []


def test_enable_rejects_expired_active_reply(wire):
    driver, node, state, _ = wire
    state["reply"] = snapshot(expires_at="2020-01-01T00:00:00.000Z")
    with pytest.raises(OrchestratorError, match="access_unavailable"):
        driver.mutate(node, REMOTE, "enable", {})


@pytest.mark.parametrize(
    "change",
    [
        {"external_id": "another-binding"},
        {"expires_at": LATER},
        {"client_id": "invalid"},
    ],
)
def test_create_rejects_wrong_assignment_reply(wire, change):
    driver, node, state, _ = wire
    state["reply"] = snapshot(**change)
    with pytest.raises(OrchestratorError):
        driver.create(node, NodeGrant(CLIENT["external_id"], CLIENT["name"], EXPIRY))
