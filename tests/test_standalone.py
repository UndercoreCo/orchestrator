import hashlib
import io
import json
from pathlib import Path
import tarfile
import sqlite3
import pytest

from orchestrator.bootstrap import load_settings
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.interfaces.cli import main as cli
from orchestrator.infrastructure.sqlite.assignments import Assignments
from scripts import update


@pytest.fixture
def settings_file(tmp_path):
    data = json.loads(Path("config/settings.example.json").read_text())
    token = tmp_path / "token"
    token.write_text("a" * 40)
    token.chmod(0o600)
    data["backend_token_file"] = str(token)
    data["nodes"][0]["api_key_file"] = str(token)
    data["state_directory"] = str(tmp_path / "state")
    file = tmp_path / "settings.json"
    file.write_text(json.dumps(data))
    return file


def test_private_secrets_and_safe_configuration(settings_file):
    value = load_settings(settings_file)
    from orchestrator.infrastructure.settings import validate_nodes

    assert len(validate_nodes(value)) == 1
    secret = Path(value.backend_token_file)
    secret.chmod(0o644)
    with pytest.raises(ValueError):
        load_settings(settings_file)
    assert load_settings(settings_file, secrets=False).schema_version == 1
    secret.chmod(0o600)
    link = secret.with_name("link")
    link.symlink_to(secret)
    with pytest.raises(OSError):
        read_secret(link)


@pytest.mark.parametrize("mutation", ["duplicate", "http", "unknown", "trusttunnel", "schema"])
def test_bad_configuration_is_rejected(settings_file, mutation):
    data = json.loads(settings_file.read_text())
    if mutation == "duplicate":
        data["nodes"].append(data["nodes"][0])
    if mutation == "http":
        data["nodes"][0]["api_url"] = "http://node.example.invalid"
    if mutation == "unknown":
        data["unexpected_secret"] = "sensitive"
    if mutation == "trusttunnel":
        data["nodes"][0]["protocol"] = "trusttunnel"
    if mutation == "schema":
        data["schema_version"] = 2
    settings_file.write_text(json.dumps(data))
    with pytest.raises(Exception):
        load_settings(settings_file, secrets=False)


def test_config_check_refuses_missing_pending_target(settings_file):
    data = json.loads(settings_file.read_text())
    journal = Assignments(data["state_directory"])
    with journal.db() as db:
        db.execute(
            "INSERT INTO switches (client_id,operation_id,source_node,target_node,target_server,binding_key,name,expires_at,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "client",
                "op",
                "node-a",
                "missing",
                "server-missing",
                "binding",
                "Test",
                "2099-01-01T00:00:00.000Z",
                "prepared",
                0,
            ),
        )
    args = cli.parser().parse_args(["--settings", str(settings_file), "check-config"])
    with pytest.raises(ValueError, match="pending target"):
        cli.run(args)


def test_cli_removes_configuration_and_opaque_error_details(monkeypatch, capsys):
    assert cli.safe_response(
        {
            "connection_id": "test",
            "configuration": {"data": "PRIVATE"},
            "nodes": [{"id": "n", "api_key": "SECRET"}],
        }
    ) == {"connection_id": "test", "nodes": [{"id": "n"}]}

    def fail(args):
        raise RuntimeError("PRIVATE RESPONSE BODY")

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main(["health"]) == 1
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "http://node.example.invalid",
        "https://user:pass@example.invalid",
        "https://example.invalid/path",
        "https://example.invalid?token=x",
    ],
)
def test_cli_rejects_unsafe_api_destinations(url):
    with pytest.raises(ValueError):
        cli.request(url, "secret", "GET", "/v1/health")


def archive(tmp_path, *, extra=None, version="0.1.0", storage=None):
    paths = [
        "VERSION",
        "requirements.lock",
        "orchestrator/runtime.py",
        "orchestrator/settings.py",
        "orchestrator/cli.py",
        "deploy/orchestratorctl",
        *(f"deploy/{u}" for u in update.UNITS),
    ]
    files = {p: b"test" for p in paths}
    files["VERSION"] = version.encode()
    if extra:
        files.update(extra)
    if storage is not None:
        files["contracts/storage.json"] = json.dumps(storage).encode()
    metadata = {
        "version": version,
        "commit": "a" * 40,
        "journal_schema": 1,
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
    }
    if storage is not None:
        metadata["storage"] = storage
    files["release.json"] = json.dumps(metadata).encode()
    path = tmp_path / "release.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, value in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(value)
            tar.addfile(item, io.BytesIO(value))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_integrity(tmp_path):
    path, digest = archive(tmp_path)
    assert update.inspect_archive(path, digest)[0]["version"] == "0.1.0"
    with pytest.raises(ValueError, match="checksum"):
        update.inspect_archive(path, "0" * 64)


@pytest.mark.parametrize(
    "name",
    ["../escape", "/etc/passwd", "config/settings.json", "orchestrator/leak.token", "private/file"],
)
def test_release_rejects_unsafe_entries(tmp_path, name):
    path, digest = archive(tmp_path, extra={name: b"unsafe"})
    with pytest.raises(ValueError):
        update.inspect_archive(path, digest)


def test_release_rejects_links(tmp_path):
    path, digest = archive(tmp_path)
    with tarfile.open(path, "w:gz") as tar:
        item = tarfile.TarInfo("orchestrator/link")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc"
        tar.addfile(item)
    with pytest.raises(ValueError, match="entry"):
        update.inspect_archive(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize(
    "outcome",
    [
        "unhealthy",
        "healthy",
        "preflight_failed",
        "stopped_preflight_failed",
        "incompatible_live_schema",
    ],
)
def test_failed_health_rolls_back_code_without_restoring_live_journal(
    tmp_path, monkeypatch, outcome
):
    root = tmp_path / "app"
    releases = root / "releases"
    previous = releases / "previous"
    previous.mkdir(parents=True)
    (root / "current").symlink_to(previous)
    units = tmp_path / "units"
    units.mkdir()
    for unit in update.UNITS:
        (units / unit).write_text("old unit")
    settings = tmp_path / "settings.json"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    journal = state / "assignments.sqlite3"
    with sqlite3.connect(journal) as db:
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker VALUES ('before')")
    settings.write_text(
        json.dumps(
            {
                "state_directory": "/var/lib/vpn-orchestrator",
                "backend_token_file": str(tmp_path / "token"),
            }
        )
    )
    (tmp_path / "token").write_text("a" * 40)
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(update, "ROOT", root)
    monkeypatch.setattr(update, "SETTINGS", settings)
    monkeypatch.setattr(update, "UNITS_DIR", units)
    monkeypatch.setattr(update, "BACKUPS", backups)
    monkeypatch.setattr(update, "CLI", tmp_path / "cli")
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    from types import SimpleNamespace

    monkeypatch.setattr(
        update.pwd, "getpwnam", lambda _: SimpleNamespace(pw_name="vpn-orchestrator")
    )
    # Only replace the fixed server state path; all snapshots and symlinks are real.
    original_path = update.Path
    monkeypatch.setattr(
        update, "Path", lambda p: state if p == "/var/lib/vpn-orchestrator" else original_path(p)
    )
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path in (root, releases, settings.parent):
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode)
        return value

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(args) > 1 and args[1] == "-c":
            count = sum(len(c) > 1 and c[1] == "-c" for c in calls)
            if (outcome == "preflight_failed" and count == 1) or (
                outcome == "stopped_preflight_failed" and count == 2
            ):
                raise RuntimeError("synthetic_preflight_failed")
        if (
            args[:3] == ["systemctl", "start", update.UNITS[0]]
            and (root / "current").resolve() != previous
        ):
            # A live write after activation must survive a failed rollout.
            with sqlite3.connect(journal) as db:
                db.execute("INSERT INTO marker VALUES ('live')")
                if outcome == "incompatible_live_schema":
                    db.execute("PRAGMA user_version=2")
        return b""

    monkeypatch.setattr(update, "run", run)
    monkeypatch.setattr(update.time, "sleep", lambda _: None)

    def unavailable(*a, **kw):
        if outcome == "healthy":
            return io.BytesIO(json.dumps({"status": "ok", "version": "0.1.0"}).encode())
        raise OSError("offline")

    monkeypatch.setattr(update.urllib.request, "urlopen", unavailable)
    path, digest = archive(tmp_path)
    if outcome == "healthy":
        assert update.update(path, digest, True)["status"] == "updated"
        assert (root / "current").resolve() != previous
        assert ["systemctl", "start", update.UNITS[1]] in calls
    else:
        with pytest.raises(
            (ValueError, RuntimeError),
            match="unhealthy|preflight_failed|rollback_schema_incompatible",
        ):
            update.update(path, digest, True)
        if outcome == "incompatible_live_schema":
            assert (root / "current").resolve() != previous
            assert sum(c == ["systemctl", "start", update.UNITS[0]] for c in calls) == 1
        else:
            assert (root / "current").resolve() == previous
        assert all((units / u).read_text() == "old unit" for u in update.UNITS)
    with sqlite3.connect(journal) as db:
        expected = [("before",)] if "preflight" in outcome else [("before",), ("live",)]
        assert db.execute("SELECT value FROM marker").fetchall() == expected
    if outcome == "preflight_failed":
        assert not any(c[0] == "systemctl" for c in calls)
    elif outcome not in ("healthy", "incompatible_live_schema"):
        assert ["systemctl", "start", update.UNITS[2]] in calls


def test_retired_admin_key_is_not_required_for_startup(settings_file):
    data = json.loads(settings_file.read_text())
    data["admin_token_file"] = str(settings_file.parent / "removed-admin.token")
    settings_file.write_text(json.dumps(data))
    assert load_settings(settings_file).admin_token_file == data["admin_token_file"]


def test_additive_upgrade_preserves_existing_journal_and_seeds_inventory(settings_file, tmp_path):
    import subprocess
    import sys

    data = json.loads(settings_file.read_text())
    store = Assignments(data["state_directory"])
    with store.db() as db:
        db.execute("PRAGMA user_version=0")  # Simulate an actual unversioned release.
        for table in ("control_leases", "access_cache", "retired_bindings"):
            db.execute("DROP TABLE " + table)
    code = next(
        c
        for c in update.update.__code__.co_consts
        if isinstance(c, str) and c.startswith("import sqlite3,sys")
    )
    subprocess.run(
        [sys.executable, "-c", code, data["state_directory"], str(settings_file)], check=True
    )
    with store.db() as db:
        assert db.execute("SELECT id,revision FROM node_registry").fetchall()[0]["id"] == "node-a"
    # A repeated initialization does not reset the live inventory revision or duplicate a node.
    with store.db() as db:
        db.execute("UPDATE node_registry SET revision=8")
    subprocess.run(
        [sys.executable, "-c", code, data["state_directory"], str(settings_file)], check=True
    )
    with store.db() as db:
        assert db.execute("SELECT revision FROM node_registry").fetchone()[0] == 8


def test_installer_restores_caller_umask_on_failure(monkeypatch):
    import importlib.util
    import os
    import sys

    monkeypatch.setitem(sys.modules, "update", update)
    spec = importlib.util.spec_from_file_location("installer", "scripts/install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)

    def fail(archive, digest):
        assert os.umask(0o022) == 0o022
        raise ValueError("test failure")

    monkeypatch.setattr(installer, "_install", fail)
    previous = os.umask(0o077)
    try:
        with pytest.raises(ValueError, match="test failure"):
            installer.install(None, None)
        assert os.umask(0o077) == 0o077
    finally:
        os.umask(previous)
