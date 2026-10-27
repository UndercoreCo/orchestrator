"""A changed policy, lost response or reboot cannot erase an issued safety bound."""

import time

import pytest

from test_node_switch import pair, export
from test_leases import enroll
from test_recovery import request
from orchestrator.application.leases import NodeLeases
from orchestrator.application.recovery import Recovery, reconcile
from orchestrator.bootstrap import build_gateway
from orchestrator.config.policy import RuntimePolicy
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.sqlite.assignments import Assignments
from storage_scenarios import promote_schema_two


@pytest.fixture(params=[1, 2])
def lease_pair(pair, request):
    if request.param == 2:
        promote_schema_two(pair[0].store)
    return pair


def extended_policy():
    return RuntimePolicy.model_validate(
        {"leases": {"max_node_lease_seconds": 300, "fence_grace_seconds": 600}}
    )


@pytest.mark.parametrize("lost_reply", [False, True])
def test_restart_and_lower_policy_cannot_shorten_issued_bound(lease_pair, monkeypatch, lost_reply):
    gateway, engines, states, _, ident, _ = lease_pair
    enroll(gateway)
    export(gateway, ident)
    gateway.policy = extended_policy()
    lease = NodeLeases(gateway)
    node = gateway.nodes["a"]
    if lost_reply:
        states["a"]["lose"] = "/v1/control-lease"
        with pytest.raises(OrchestratorError):
            lease.heartbeat(node)
    else:
        lease.heartbeat(node)
    assert lease.state(node)["fence_window_seconds"] == 900

    # Reopen SQLite as well as the service; the new policy is back at defaults.
    restarted = build_gateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    resumed = NodeLeases(restarted)
    resumed.heartbeat(node)  # A smaller successful renewal must not erase history.
    assert resumed.state(node)["fence_window_seconds"] == 900
    states["a"]["down"] = True
    clock = time.monotonic()
    monkeypatch.setattr("orchestrator.application.leases.time.monotonic", lambda: clock)
    with pytest.raises(OrchestratorError, match="lease_waiting"):
        Recovery(restarted).recover(ident, request())
    deadline = resumed.state(node)["fence_after"]
    assert deadline == clock + 900
    clock += 246  # The old default fence would have allowed a second active peer.
    assert reconcile(restarted)["completed"] == 0
    assert not engines["b"].rows()
    clock = deadline - 1
    assert not resumed.expired(node)
    # Use the persisted boundary: summing fractional monotonic values in a
    # different order can leave the fake clock one floating-point ULP early.
    clock = deadline
    assert resumed.expired(node)


def test_boot_change_restarts_full_persisted_wait_without_sliding_on_poll(lease_pair, monkeypatch):
    gateway, _, _, _, _, _ = lease_pair
    gateway.policy = extended_policy()
    enroll(gateway)
    node = gateway.nodes["a"]
    lease = NodeLeases(gateway)
    lease.fence(node)
    gateway.policy = RuntimePolicy()
    gateway.boot_id = "different-test-boot"
    clock = time.monotonic()
    monkeypatch.setattr("orchestrator.application.leases.time.monotonic", lambda: clock)
    assert not lease.expired(node)
    deadline = lease.state(node)["fence_after"]
    assert deadline == clock + 900
    clock = deadline - 1
    lease.fence(node)
    assert lease.state(node)["fence_after"] == deadline
    assert not lease.expired(node)
    clock = deadline
    assert lease.expired(node)


def test_larger_fencing_policy_is_durable_even_without_renewal(lease_pair):
    gateway, _, _, _, _, _ = lease_pair
    enroll(gateway)
    node = gateway.nodes["a"]
    lease = NodeLeases(gateway)
    with gateway.store.db() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert lease.state(node)["fence_window_seconds"] == (245 if version == 2 else 900)
    gateway.policy = extended_policy()
    lease.fence(node)
    assert lease.state(node)["fence_window_seconds"] == 900
    gateway.policy = RuntimePolicy()
    gateway.boot_id = "next-test-boot"
    before = time.monotonic()
    assert not lease.expired(node)
    assert lease.state(node)["fence_after"] >= before + 900


def test_confirmed_revocation_does_not_wait_for_persisted_bound(lease_pair):
    gateway, engines, _, _, ident, _ = lease_pair
    gateway.policy = extended_policy()
    enroll(gateway)
    before = export(gateway, ident)
    after = Recovery(gateway).recover(ident, request(before.revision))
    assert after.node_id == "b"
    assert not engines["a"].backend.peers
    assert len(engines["b"].backend.peers) == 1
    assert not NodeLeases(gateway).state(gateway.nodes["a"])["fenced"]


def test_failed_reservation_never_sends_permission(lease_pair, monkeypatch):
    gateway, engines, _, _, _, _ = lease_pair
    enroll(gateway)
    sequence = engines["a"].control_lease.describe()["sequence"]

    def unavailable(*args):
        raise OSError("synthetic journal write failure")

    monkeypatch.setattr(gateway.store.leases, "reserve", unavailable)
    with pytest.raises(OSError):
        NodeLeases(gateway).heartbeat(gateway.nodes["a"])
    assert engines["a"].control_lease.describe()["sequence"] == sequence
