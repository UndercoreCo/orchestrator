"""Bridge release keeps legacy storage and can resume a newer live journal."""

import json
import time

import pytest

from test_node_switch import pair, export
from test_leases import enroll
from test_recovery import request
from test_storage_upgrade import logical_snapshot
from storage_scenarios import promote_schema_two, bridge_runtime, bridge_manifest
from orchestrator.application.leases import NodeLeases
from orchestrator.application.recovery import Recovery
from orchestrator.bootstrap import build_gateway
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.sqlite.upgrade import rehearse
from orchestrator.infrastructure.sqlite.migrations import validate_schema
from scripts.update import check_storage


@pytest.fixture(autouse=True)
def run_bridge_code(monkeypatch):
    with bridge_runtime():
        from orchestrator.application.leases import NodeLeases as BridgeLeases

        monkeypatch.setitem(globals(), "NodeLeases", BridgeLeases)
        yield


def test_old_fence_requires_full_legacy_wait_once(pair, monkeypatch):
    gateway, _, _, _, _, _ = pair
    enroll(gateway)
    node = gateway.nodes["a"]
    with gateway.store.db() as db:
        db.execute(
            "UPDATE control_leases SET fenced=1,fence_boot=?,fence_after=0 WHERE node_id='a'",
            (gateway.boot_id,),
        )
    clock = time.monotonic()
    monkeypatch.setattr("orchestrator.application.leases.time.monotonic", lambda: clock)
    lease = NodeLeases(gateway)
    assert not lease.expired(node)
    deadline = lease.state(node)["fence_after"]
    assert deadline == clock + 900
    restarted = build_gateway(list(gateway.nodes.values()), gateway.store, gateway.resources)
    restarted.boot_id = gateway.boot_id  # Model a service restart within one Linux boot.
    clock = deadline - 1
    assert not NodeLeases(restarted).expired(node)
    assert NodeLeases(restarted).state(node)["fence_after"] == deadline
    clock = deadline
    assert NodeLeases(restarted).expired(node)
    with gateway.store.db() as db:
        validate_schema(db, version=1)


def test_schema_one_rejects_unrepresentable_permission_before_write(pair):
    gateway, _, _, _, _, _ = pair
    enroll(gateway)
    before = logical_snapshot(gateway.store.path)
    with pytest.raises(ValueError, match="lease_window_requires_schema_upgrade"):
        gateway.store.leases.reserve("a", "a-identity", "controller", 99, 9999999999, 901)
    assert logical_snapshot(gateway.store.path) == before


def test_rollback_preserves_larger_bound_and_live_denial(pair, monkeypatch):
    gateway, engines, states, _, ident, _ = pair
    enroll(gateway)
    before = export(gateway, ident)
    promote_schema_two(gateway.store)  # Planned producer release, not bridge startup.
    with gateway.store.db() as db:
        db.execute("UPDATE control_leases SET fence_window_seconds=3600 WHERE node_id='a'")
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError, match="lease_waiting"):
        Recovery(gateway).recover(ident, request(before.revision))
    with pytest.raises(OrchestratorError):
        gateway.client(ident, "disable")
    pending = gateway.store.switches.pending(ident)
    snapshot = logical_snapshot(gateway.store.path)
    rehearse(gateway.store.path.parent)
    restarted = build_gateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    assert logical_snapshot(gateway.store.path) == snapshot
    assert restarted.store.switches.pending(ident) == pending
    assert pending["disable_requested"] == 1
    assert restarted.store.leases.cached(ident)["denied"] == 1
    restarted.boot_id = "rollback-test-boot"
    lease = NodeLeases(restarted)
    node = restarted.nodes["a"]
    clock = time.monotonic()
    monkeypatch.setattr("orchestrator.application.leases.time.monotonic", lambda: clock)
    assert not lease.expired(node)
    assert lease.state(node)["fence_after"] == clock + 3600
    clock += 901
    assert not lease.expired(node)
    with pytest.raises(OrchestratorError):
        export(restarted, ident)
    assert not engines["b"].backend.peers
    assert not engines["b"].rows()


def test_storage_manifest_allows_two_step_upgrade_but_never_unsafe_downgrade(tmp_path):
    from pathlib import Path

    bridge = {"storage": bridge_manifest()}
    old = {"storage": {"min_read": 0, "max_read": 1, "write": 1}}
    future = {"storage": {"min_read": 0, "max_read": 2, "write": 2}}
    store = Assignments(tmp_path / "state")
    check_storage(store.path, bridge, old)
    check_storage(store.path, future, bridge)
    with pytest.raises(ValueError, match="rollback_schema_incompatible"):
        check_storage(store.path, future, old)
    promote_schema_two(store)
    check_storage(store.path, bridge, future)
    with pytest.raises(ValueError, match="journal_schema_unsupported"):
        check_storage(store.path, old, bridge)
