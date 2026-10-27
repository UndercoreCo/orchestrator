"""Private verified snapshots; restoration only prepares a separate offline directory."""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from datetime import datetime, timezone

from orchestrator.bootstrap import load_settings
from orchestrator.config.settings import Settings
from orchestrator.domain.inventory import NodeSettings
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.infrastructure.sqlite.inventory import read_inventory
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.sqlite.migrations import MAX_READ_SCHEMA, schema_version
from orchestrator.version import current_version


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def encode(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def backup(settings_path, destination):
    settings = load_settings(settings_path)
    source = Path(settings.state_directory) / "assignments.sqlite3"
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    target = destination / "assignments.sqlite3"
    write_private(target, b"")
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as snapshot,
    ):
        original.backup(snapshot)
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Invalid snapshot")
        version = schema_version(snapshot)
        if not 0 <= version <= MAX_READ_SCHEMA:
            raise ValueError("journal_schema_unsupported")
    audit_source = Path(settings.state_directory) / "operator.sqlite3"
    if audit_source.exists():
        audit_target = destination / "operator.sqlite3"
        write_private(audit_target, b"")
        with (
            sqlite3.connect(audit_source.as_uri() + "?mode=ro", uri=True) as original,
            sqlite3.connect(audit_target) as snapshot,
        ):
            original.backup(snapshot)
            if (
                snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or snapshot.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise ValueError("audit_snapshot_invalid")
    nodes = read_inventory(target)
    if nodes is None:
        nodes = settings.nodes
    paths = {settings.backend_token_file, *(node.api_key_file for node in nodes)}
    mapping = {}
    for index, path in enumerate(sorted(paths)):
        filename = f"secret-{index}.token"
        write_private(destination / filename, read_secret(path))
        mapping[filename] = path
    # The bootstrap inventory is needed when a snapshot predates registry seeding.
    saved = settings.model_copy(update={"nodes": nodes})
    write_private(
        destination / "settings.json", encode(saved.model_dump(exclude={"admin_token_file"}))
    )
    write_private(destination / "restore-map.json", encode(mapping))
    manifest = {
        "format_version": 1,
        "application_version": current_version(),
        "journal_version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(destination.iterdir())
        },
    }
    # Written last: an interrupted snapshot is never accepted as a complete backup.
    write_private(destination / "backup.json", encode(manifest))
    return {"status": "backed_up", "directory": str(destination), "journal_version": version}


def verified_files(source):
    source = Path(source).absolute()
    if source.is_symlink() or source.stat().st_mode & 0o077:
        raise ValueError("private_backup_required")
    files = {}
    for path in source.iterdir():
        if (
            path.is_symlink()
            or not stat.S_ISREG(path.stat().st_mode)
            or path.stat().st_mode & 0o077
        ):
            raise ValueError("private_backup_required")
        if path.name not in {
            "backup.json",
            "settings.json",
            "assignments.sqlite3",
            "operator.sqlite3",
            "restore-map.json",
        } and not re.fullmatch(r"secret-[0-9]+\.token", path.name):
            raise ValueError("backup_file_invalid")
        files[path.name] = path.read_bytes()
    manifest = json.loads(files.pop("backup.json"))
    if manifest.get("format_version") != 1 or type(manifest.get("format_version")) is not int:
        raise ValueError("backup_version_unsupported")
    if manifest.get("files") != {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    }:
        raise ValueError("backup_checksum_invalid")
    version = manifest.get("journal_version")
    if type(version) is not int or not 0 <= version <= MAX_READ_SCHEMA:
        raise ValueError("journal_schema_unsupported")
    return files, version


def restore_backup(source, destination):
    files, version = verified_files(source)
    settings = Settings.model_validate_json(files["settings.json"])
    mapping = json.loads(files["restore-map.json"])
    secrets = {name for name in files if name.startswith("secret-")}
    if (
        not isinstance(mapping, dict)
        or set(mapping) != secrets
        or any(not isinstance(p, str) for p in mapping.values())
        or len(set(mapping.values())) != len(mapping)
    ):
        raise ValueError("backup_mapping_invalid")
    destination = Path(destination).absolute()
    # No overwrite flag: neither a running installation nor an earlier attempt is replaced.
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = destination / "state"
    state.mkdir(mode=0o700)
    # Fail closed even if a later validation fails and leaves a partial directory.
    write_private(
        state / ".restore-review-required",
        b"Reconcile snapshot with VPN nodes before activation.\n",
    )
    remap = {old: str(destination / name) for name, old in mapping.items()}
    for name in secrets:
        write_private(destination / name, files[name])
    target = state / "assignments.sqlite3"
    write_private(target, files["assignments.sqlite3"])
    with sqlite3.connect(target) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or schema_version(db) != version
        ):
            raise ValueError("backup_journal_invalid")
    if "operator.sqlite3" in files:
        audit_target = state / "operator.sqlite3"
        write_private(audit_target, files["operator.sqlite3"])
        with sqlite3.connect(audit_target) as audit:
            if (
                audit.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or audit.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise ValueError("backup_audit_invalid")
    store = Assignments(state)
    with store.db() as db:
        for node_id, configuration in db.execute(
            "SELECT id,configuration FROM node_registry"
        ).fetchall():
            node = NodeSettings.model_validate_json(configuration)
            node.api_key_file = remap[node.api_key_file]
            db.execute(
                "UPDATE node_registry SET configuration=? WHERE id=?",
                (node.model_dump_json(), node_id),
            )
    for node in settings.nodes:
        node.api_key_file = remap[node.api_key_file]
    settings.state_directory = str(state)
    settings.backend_token_file = remap[settings.backend_token_file]
    settings.admin_token_file = None
    write_private(
        destination / "settings.json", encode(settings.model_dump(exclude={"admin_token_file"}))
    )
    with store.db() as db:
        restored_version = schema_version(db)
    return {
        "status": "restored_for_review",
        "directory": str(destination),
        "settings": str(destination / "settings.json"),
        "journal_version": restored_version,
    }
