"""Real updater/files/SQLite; systemd and dependency installation are simulated."""

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import update
from test_standalone import archive
from storage_scenarios import bridge_runtime, bridge_manifest
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.sqlite.upgrade import rehearse


@pytest.mark.parametrize("transition", ["old_to_bridge", "bridge_to_schema_two"])
@pytest.mark.parametrize("outcome", ["healthy", "unhealthy", "preflight_failed"])
def test_bridge_update_keeps_live_revocation_and_sequence(
    tmp_path, monkeypatch, transition, outcome
):
    root = tmp_path / "app"
    previous = root / "releases" / "previous"
    previous.mkdir(parents=True)
    (root / "current").symlink_to(previous)
    old = {"min_read": 0, "max_read": 1, "write": 1}
    bridge = bridge_manifest()
    target = bridge if transition == "old_to_bridge" else {**bridge, "write": 2}
    (previous / "release.json").write_text(
        json.dumps(
            {
                "version": "0.5.1" if transition == "old_to_bridge" else "0.5.2",
                "commit": "b" * 40,
                "journal_schema": 2,
                "storage": old if transition == "old_to_bridge" else bridge,
            }
        )
    )
    units = tmp_path / "units"
    units.mkdir()
    for unit in update.UNITS:
        (units / unit).write_text("old unit")
    state = tmp_path / "state"
    with bridge_runtime():
        store = Assignments(state)
    store.leases.reserve("node", "identity", "controller", 1, 9999999999, 245)
    store.leases.verify("node")
    with store.db() as db:
        db.execute("INSERT INTO access_cache VALUES ('connection','binding','{}',0)")
    token = tmp_path / "token"
    token.write_text("t" * 40)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "state_directory": "/var/lib/vpn-orchestrator",
                "backend_token_file": str(token),
            }
        )
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    for name, value in {
        "ROOT": root,
        "SETTINGS": settings,
        "UNITS_DIR": units,
        "BACKUPS": backups,
        "CLI": tmp_path / "cli",
    }.items():
        monkeypatch.setattr(update, name, value)
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        update.pwd, "getpwnam", lambda _: SimpleNamespace(pw_name="vpn-orchestrator")
    )
    monkeypatch.setattr(
        update, "Path", lambda p: state if p == "/var/lib/vpn-orchestrator" else Path(p)
    )
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path in (root, root / "releases", settings.parent):
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode)
        return value

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(args) > 1 and args[1] == "-c":
            if outcome == "preflight_failed":
                raise RuntimeError("synthetic_preflight_failed")
            if target["write"] == 2:
                rehearse(args[3])  # Actual producer migration on the copied journal.
            else:
                with bridge_runtime():
                    rehearse(args[3])
        if args[:3] == ["systemctl", "start", update.UNITS[0]]:
            if (root / "current").resolve() != previous:
                if target["write"] == 2:
                    Assignments(state)  # Actual producer startup, not a simulated ALTER.
                # Mutations performed after activation must survive failed health
                # and code rollback; restoring the preflight database is unsafe.
                with store.db() as db:
                    db.execute("UPDATE access_cache SET denied=1")
                store.leases.reserve(
                    "node",
                    "identity",
                    "controller",
                    2,
                    9999999999,
                    3600 if target["write"] == 2 else 245,
                )
            else:
                # Execute the bridge's real initializer against the live schema
                # produced above. It must neither migrate nor replace the state.
                with bridge_runtime():
                    reopened = Assignments(state)
                assert reopened.leases.state("node")["sequence"] == 2
                assert reopened.leases.cached("connection")["denied"] == 1
        return b""

    monkeypatch.setattr(update, "run", run)
    monkeypatch.setattr(update.time, "sleep", lambda _: None)

    target_version = "0.5.2" if transition == "old_to_bridge" else "0.5.3"

    def health(*args, **kwargs):
        if outcome == "healthy":
            return io.BytesIO(json.dumps({"status": "ok", "version": target_version}).encode())
        raise OSError("synthetic health failure")

    monkeypatch.setattr(update.urllib.request, "urlopen", health)
    path, digest = archive(tmp_path, version=target_version, storage=target)
    if outcome == "healthy":
        assert update.update(path, digest)["status"] == "updated"
        assert (root / "current").resolve() != previous
    else:
        with pytest.raises((ValueError, RuntimeError), match="unhealthy|preflight_failed"):
            update.update(path, digest)
        assert (root / "current").resolve() == previous
    activated = outcome != "preflight_failed"
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (
            target["write"] if activated else 1
        )
        assert db.execute("SELECT denied FROM access_cache").fetchone()[0] == int(activated)
        assert db.execute("SELECT sequence FROM control_leases").fetchone()[0] == (
            2 if activated else 1
        )
    if activated and target["write"] == 2:
        assert store.leases.state("node")["fence_window_seconds"] == 3600
    if outcome == "preflight_failed":
        assert not any(c[0] == "systemctl" for c in calls)
