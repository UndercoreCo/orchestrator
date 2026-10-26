"""Real schema activation and pinned bridge rollback without contacting live nodes."""

import time

import pytest

from test_node_switch import pair, export
from test_leases import enroll
from test_recovery import request as recovery_request
from test_storage_upgrade import logical_snapshot
from storage_scenarios import bridge_runtime
from orchestrator.bootstrap import build_gateway
from orchestrator.application.recovery import Recovery
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.sqlite import upgrade


@pytest.fixture
def legacy_pair(request):
    with bridge_runtime():
        return request.getfixturevalue("pair")


def test_activate_then_bridge_rollback_keeps_pending_denial_and_full_wait(legacy_pair, monkeypatch):
    gateway, engines, states, _, ident, _ = legacy_pair
    with bridge_runtime():
        enroll(gateway)
        before = export(gateway, ident)
        states["a"]["down"] = True
        with pytest.raises(OrchestratorError, match="lease_waiting"):
            Recovery(gateway).recover(ident, recovery_request(before.revision))
    pending = gateway.store.switches.pending(ident)
    assert logical_snapshot(gateway.store.path)[0] == 1
    upgrade.rehearse(gateway.store.path.parent)  # Real producer migrates this test journal.
    producer = build_gateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    assert logical_snapshot(producer.store.path)[0] == 2
    assert producer.store.switches.pending(ident) == pending
    assert producer.store.leases.state("a")["fence_boot"] == ""
    # Model a grant with a larger bound and a revocation after migration. Neither
    # may be lost when health fails and the updater restores the bridge code.
    clock = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: clock)
    producer.store.leases.reserve("a", "a-identity", "controller", 99, 9999999999, 3600)
    with pytest.raises(OrchestratorError):
        producer.client(ident, "disable")
    snapshot = logical_snapshot(producer.store.path)
    with bridge_runtime():
        from orchestrator.application.leases import NodeLeases

        rolled_back = build_gateway(
            list(gateway.nodes.values()), Assignments(producer.store.path.parent), gateway.resources
        )
        assert logical_snapshot(producer.store.path) == snapshot
        assert rolled_back.store.switches.pending(ident)["disable_requested"] == 1
        assert rolled_back.store.leases.cached(ident)["denied"] == 1
        lease = NodeLeases(rolled_back)
        node = rolled_back.nodes["a"]
        assert not lease.expired(node)
        deadline = lease.state(node)["fence_after"]
        assert deadline == clock + 3600
        clock += 901
        assert not lease.expired(node)
        with pytest.raises(OrchestratorError):
            export(rolled_back, ident)
        assert not engines["b"].rows()
        clock = deadline
        assert lease.expired(node)


@pytest.mark.parametrize(
    "damage",
    ["sequence", "valid_until", "server_id", "fenced", "denied", "window", "boot"],
)
def test_rehearsal_rejects_unrelated_changes(tmp_path, monkeypatch, damage):
    with bridge_runtime():
        store = Assignments(tmp_path / "state")
    store.leases.reserve("node", "identity", "controller", 9, 9999999999, 245)
    store.leases.fence("node", "boot", 1000, 900)
    with store.db() as db:
        db.execute("INSERT INTO access_cache VALUES ('connection','binding','{}',1)")
    mutations = {
        "sequence": "UPDATE control_leases SET sequence=0",
        "valid_until": "UPDATE control_leases SET valid_until=0",
        "server_id": "UPDATE control_leases SET server_id='other'",
        "fenced": "UPDATE control_leases SET fenced=0",
        "denied": "UPDATE access_cache SET denied=0",
        "window": "UPDATE control_leases SET fence_window_seconds=245",
        "boot": "UPDATE control_leases SET fence_boot='unexpected'",
    }

    def corrupted_migration(directory):
        migrated = Assignments(directory)
        with migrated.db() as db:
            db.execute(mutations[damage])

    monkeypatch.setattr(upgrade, "Assignments", corrupted_migration)
    with pytest.raises(ValueError, match="migration_changed_existing_rows"):
        upgrade.rehearse(store.path.parent)


@pytest.mark.parametrize("version,fenced", [(1, 0), (2, 1)])
def test_rehearsal_does_not_allow_reset_outside_legacy_fenced_row(
    tmp_path, monkeypatch, version, fenced
):
    if version == 1:
        with bridge_runtime():
            store = Assignments(tmp_path / "state")
    else:
        store = Assignments(tmp_path / "state")
    store.leases.reserve("node", "identity", "controller", 1, 9999999999, 245)
    with store.db() as db:
        db.execute("UPDATE control_leases SET fenced=?,fence_boot='keep'", (fenced,))

    def corrupted_migration(directory):
        migrated = Assignments(directory)
        with migrated.db() as db:
            db.execute("UPDATE control_leases SET fence_boot=''")

    monkeypatch.setattr(upgrade, "Assignments", corrupted_migration)
    with pytest.raises(ValueError, match="migration_changed_existing_rows"):
        upgrade.rehearse(store.path.parent)
