"""Version/migration matrix, atomic failures and offline restore lifecycle."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_node_switch import pair
from orchestrator.bootstrap import build_gateway, load_settings
from orchestrator.application.connections import Connections
from orchestrator.domain.inventory import NodeSettings
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.sqlite import migrations
from orchestrator.infrastructure.sqlite.upgrade import rehearse
from orchestrator.interfaces.cli.backup import backup, restore_backup
from scripts import update
from storage_scenarios import promote_schema_two


def logical_snapshot(path):
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0], list(db.iterdump())


@pytest.mark.parametrize("layout", ["empty", "initial", "connections", "leases", "registry"])
def test_supported_unversioned_layouts_and_repeat(tmp_path, layout):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "assignments.sqlite3"
    if layout in ("initial", "connections", "leases", "registry"):
        store = Assignments(state)
        with store.db() as db:
            db.execute("PRAGMA user_version=0")
            db.execute(
                "INSERT INTO assignments(external_id,client_id,node_id,server_id,creation_expires_at,remote_id,created_at,device_id,binding_key) VALUES('device','connection','node','identity','2099-01-01T00:00:00.000Z','remote',1,'device','device')"
            )
            if layout != "registry":
                for table in ("node_registry", "registry_meta"):
                    db.execute("DROP TABLE " + table)
            if layout in ("initial", "connections"):
                for table in ("control_leases", "access_cache", "retired_bindings"):
                    db.execute("DROP TABLE " + table)
            if layout == "initial":
                db.execute("DROP TABLE switches")
                db.execute("DROP INDEX assignments_device_protocol")
                for column in (
                    "device_id",
                    "protocol",
                    "configuration_revision",
                    "configuration_digest",
                    "binding_key",
                ):
                    db.execute("ALTER TABLE assignments DROP COLUMN " + column)
    upgraded = Assignments(state)
    assert logical_snapshot(path)[0] == migrations.CURRENT_SCHEMA
    if layout != "empty":
        row = upgraded.get(client_id="connection")
        assert row["remote_id"] == "remote"
        assert row["device_id"] == row["binding_key"] == "device"
    before = logical_snapshot(path)
    Assignments(state)
    assert logical_snapshot(path) == before


@pytest.mark.parametrize("phase", ["ddl", "version"])
def test_failed_migration_rolls_back_ddl_backfill_and_version(tmp_path, monkeypatch, phase):
    state = tmp_path / "state"
    store = Assignments(state)
    with store.db() as db:
        db.execute("PRAGMA user_version=0")
        db.execute("DROP TABLE retired_bindings")
    before = logical_snapshot(store.path)
    original = migrations._baseline

    def fail(db):
        original(db)
        # Only fail the real journal, not in-memory schema verification.
        if db.execute("PRAGMA database_list").fetchone()[2]:
            db.execute("CREATE TABLE partial(value TEXT)")
            raise RuntimeError("synthetic interruption")

    if phase == "ddl":
        monkeypatch.setattr(migrations, "_baseline", fail)
    else:
        validate = migrations.validate_schema

        def fail_validation(db):
            assert migrations.schema_version(db) == migrations.CURRENT_SCHEMA
            raise RuntimeError("synthetic failure after version change")

        monkeypatch.setattr(migrations, "validate_schema", fail_validation)
    with pytest.raises(RuntimeError):
        Assignments(state)
    assert logical_snapshot(store.path) == before
    monkeypatch.setattr(migrations, "_baseline", original)
    if phase == "version":
        monkeypatch.setattr(migrations, "validate_schema", validate)
    Assignments(state)
    assert logical_snapshot(store.path)[0] == migrations.CURRENT_SCHEMA


@pytest.mark.parametrize("damage", ["future", "missing_table", "wrong_index", "foreign"])
def test_unsupported_or_damaged_database_is_not_repaired(tmp_path, damage):
    store = Assignments(tmp_path / "state")
    with store.db() as db:
        if damage == "future":
            db.execute("PRAGMA user_version=99")
        elif damage == "missing_table":
            db.execute("DROP TABLE access_cache")
        elif damage == "wrong_index":
            db.execute("DROP INDEX assignments_device_protocol")
            db.execute("CREATE INDEX assignments_device_protocol ON assignments(protocol)")
        else:
            db.execute("PRAGMA user_version=0")
            db.execute("CREATE TABLE foreign_data(value TEXT)")
    before = logical_snapshot(store.path)
    with pytest.raises(ValueError, match="schema"):
        Assignments(store.path.parent)
    assert logical_snapshot(store.path) == before


def test_concurrent_initialization_commits_one_version(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: Assignments(tmp_path / "state"), range(4)))
    assert logical_snapshot(stores[0].path)[0] == migrations.CURRENT_SCHEMA


@pytest.mark.parametrize("version", [0, 1])
def test_legacy_lease_migration_uses_maximum_old_policy_and_preserves_access(pair, version):
    gateway, _, _, _, ident, _ = pair
    store = gateway.store
    row = store.get(client_id=ident)
    store.leases.deny(row, True)
    store.leases.retire(row)
    store.leases.reserve("a", "a-identity", "controller", 9, 9999999999, 245)
    store.leases.verify("a")
    with store.db() as db:
        db.execute("UPDATE control_leases SET fenced=1,fence_boot='old-boot',fence_after=12345")
        db.execute(f"PRAGMA user_version={version}")
    upgraded = Assignments(store.path.parent)
    state = upgraded.leases.state("a")
    assert state["fence_window_seconds"] == 900
    assert state["fenced"] == state["verified"] == 1
    assert state["fence_boot"] == ""  # First fence/expired starts a full new wait.
    assert state["sequence"] == 9
    assert state["valid_until"] == 9999999999
    assert upgraded.get(client_id=ident) == row
    assert upgraded.leases.cached(ident)["denied"] == 1
    assert upgraded.leases.restore_snapshot("a")[1][0]["remote_id"] == row["remote_id"]
    before = logical_snapshot(store.path)
    Assignments(store.path.parent)
    assert logical_snapshot(store.path) == before


def test_bridge_does_not_migrate_schema_one(tmp_path, monkeypatch):
    store = Assignments(tmp_path / "state")
    store.leases.reserve("a", "identity", "controller", 1, 9999999999, 245)
    store.leases.fence("a", "boot", 12345, 900)
    before = logical_snapshot(store.path)

    def fail(db):
        raise AssertionError("bridge must not apply schema-2 migration")

    monkeypatch.setattr(migrations, "_durable_fence_window", fail)
    Assignments(store.path.parent)
    assert logical_snapshot(store.path) == before
    assert Assignments(store.path.parent).leases.state("a")["fence_window_seconds"] == 900


@pytest.mark.parametrize("damage", ["missing_table", "wrong_index"])
def test_schema_one_is_validated_before_migration(tmp_path, damage):
    store = Assignments(tmp_path / "state")
    with store.db() as db:
        db.execute("PRAGMA user_version=1")
        if damage == "missing_table":
            db.execute("DROP TABLE access_cache")
        else:
            db.execute("DROP INDEX assignments_device_protocol")
            db.execute("CREATE INDEX assignments_device_protocol ON assignments(protocol)")
    before = logical_snapshot(store.path)
    with pytest.raises(ValueError, match="journal_schema_invalid"):
        Assignments(store.path.parent)
    assert logical_snapshot(store.path) == before


def make_settings(gateway, tmp_path):
    key = tmp_path / "node.token"
    key.write_text("n" * 40)
    key.chmod(0o600)
    backend = tmp_path / "backend.token"
    backend.write_text("b" * 40)
    backend.chmod(0o600)
    nodes = [
        NodeSettings(
            id=n.id,
            api_url=n.api_url,
            server_id=n.server_id,
            region=n.region,
            capacity=n.capacity,
            mode=n.mode,
            api_key_file=str(key),
        )
        for n in gateway.nodes.values()
    ]
    gateway.store.inventory.seed(nodes)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "state_directory": str(gateway.store.path.parent),
                "backend_token_file": str(backend),
                "nodes": [n.model_dump() for n in nodes],
            }
        )
    )
    settings.chmod(0o600)
    return settings


@pytest.mark.parametrize("version", [1, 2])
def test_backup_restore_preserves_pending_switch_denial_and_credentials(pair, tmp_path, version):
    gateway, engines, states, _, ident, request = pair
    if version == 2:
        promote_schema_two(gateway.store)
    states["b"]["lose"] = "/v1/clients"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, request)
    row = gateway.store.get(client_id=ident)
    pending = gateway.store.switches.pending(ident)
    # Persist an independent denied record and fencing metadata, not just a happy-path row.
    gateway.store.leases.deny(row, True)
    gateway.store.leases.reserve("a", "a-identity", "controller", 9, 9999999999, 700)
    settings = make_settings(gateway, tmp_path)
    saved = tmp_path / "backup"
    backup(settings, saved)
    restored = tmp_path / "restored"
    result = restore_backup(saved, restored)
    assert result["status"] == "restored_for_review"
    with pytest.raises(ValueError, match="restore_review_required"):
        load_settings(result["settings"])
    store = Assignments(restored / "state")
    assert store.get(client_id=ident) == row
    assert store.switches.pending(ident) == pending
    assert store.leases.cached(ident)["denied"] == 1
    assert store.leases.state("a")["sequence"] == 9
    assert store.leases.state("a")["fence_window_seconds"] == (700 if version == 2 else 900)
    assert result["journal_version"] == version
    data = json.loads(Path(result["settings"]).read_text())
    assert Path(data["backend_token_file"]).read_text() == "b" * 40
    assert all(Path(n["api_key_file"]).parent == restored for n in data["nodes"])
    # Same controlled agents, no writes since snapshot: simulate reviewed recovery.
    recovered = build_gateway(list(gateway.nodes.values()), store, gateway.resources)
    assert recovered.client(ident, "disable")["status"] == "disabled"
    assert not engines["a"].backend.peers and not engines["b"].backend.peers
    assert len(engines["b"].rows()) == 1
    with pytest.raises(FileExistsError):
        restore_backup(saved, restored)


@pytest.mark.parametrize("damage", ["checksum", "missing", "symlink", "version"])
def test_bad_backup_never_creates_output(pair, tmp_path, damage):
    gateway, _, _, _, _, _ = pair
    saved = tmp_path / "backup"
    backup(make_settings(gateway, tmp_path), saved)
    if damage == "checksum":
        (saved / "settings.json").write_text("{}")
    elif damage == "missing":
        (saved / "backup.json").unlink()
    elif damage == "symlink":
        (saved / "secret-0.token").unlink()
        (saved / "secret-0.token").symlink_to(tmp_path / "backend.token")
    else:
        path = saved / "backup.json"
        data = json.loads(path.read_text())
        data["journal_version"] = 99
        path.write_text(json.dumps(data))
    with pytest.raises((ValueError, KeyError)):
        restore_backup(saved, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_release_storage_contract_and_rollback_bounds(tmp_path):
    store = Assignments(tmp_path / "state")
    current = {"storage": {"min_read": 0, "max_read": 2, "write": 2}}
    update.check_storage(store.path, current, current)
    old = {"storage": {"min_read": 0, "max_read": 1, "write": 1}}
    # Unlike the first additive migration, old releases cannot safely ignore
    # durable fencing bounds. Neither implicit nor explicit rollback is allowed.
    for previous in ({}, old):
        with pytest.raises(ValueError, match="rollback_schema_incompatible"):
            update.check_storage(store.path, current, previous)
    promote_schema_two(store)
    with pytest.raises(ValueError, match="journal_schema_unsupported"):
        update.check_storage(store.path, old, current)
    future = {"storage": {"min_read": 0, "max_read": 3, "write": 3}}
    with pytest.raises(ValueError, match="rollback_schema_incompatible"):
        update.check_storage(store.path, future, current)
    with store.db() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="journal_schema_unsupported"):
        update.check_storage(store.path, current, current)
    with pytest.raises(ValueError, match="release_storage_invalid"):
        update.storage_contract({"storage": {"min_read": False, "max_read": 1, "write": 1}})


def test_backup_before_inventory_seed_retains_bootstrap_nodes(pair, tmp_path):
    gateway, _, _, _, _, _ = pair
    settings = make_settings(gateway, tmp_path)
    with gateway.store.db() as db:
        db.execute("DELETE FROM node_registry")
        db.execute("DELETE FROM registry_meta")
    saved = tmp_path / "backup"
    backup(settings, saved)
    result = restore_backup(saved, tmp_path / "restored")
    data = json.loads(Path(result["settings"]).read_text())
    assert len(data["nodes"]) == 2
    assert all(Path(n["api_key_file"]).read_text() == "n" * 40 for n in data["nodes"])


def test_rehearsal_keeps_pending_access_state(pair):
    gateway, _, states, _, ident, request = pair
    states["b"]["down"] = True
    # Reserve a target before simulating the unavailable API on its create path.
    states["b"]["down"] = False
    states["b"]["lose"] = "/v1/clients"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, request)
    before = gateway.store.switches.pending(ident)
    with gateway.store.db() as db:
        db.execute("PRAGMA user_version=0")
    rehearse(gateway.store.path.parent)
    assert gateway.store.switches.pending(ident) == before


def test_release_builder_uses_storage_contract_from_packaged_commit(tmp_path, monkeypatch, capsys):
    import hashlib
    import sys
    import tarfile
    from scripts import build_release
    from test_standalone import archive

    source, _ = archive(tmp_path)
    with tarfile.open(source) as tar:
        files = {
            entry.name: tar.extractfile(entry).read()
            for entry in tar
            if entry.name != "release.json"
        }
    files["contracts/storage.json"] = b'{"min_read":0,"max_read":1,"write":1}'
    commit = "f" * 40

    def git(*args):
        if args[0] == "rev-parse":
            return commit.encode()
        if args[0] == "ls-tree":
            return "\0".join(files).encode()
        assert args[0] == "show" and args[1].startswith(commit + ":")
        return files[args[1].split(":", 1)[1]]

    monkeypatch.setattr(build_release, "git", git)
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_release.py", "--ref", "reviewed", "--output", str(tmp_path / "release")],
    )
    build_release.main()
    output = json.loads(capsys.readouterr().out)
    path = Path(output["archive"])
    info, packaged = update.inspect_archive(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert info["storage"] == json.loads(packaged["contracts/storage.json"])
    assert info["commit"] == commit
