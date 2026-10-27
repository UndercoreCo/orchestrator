"""Versioned additive migration. DDL, backfill and version commit atomically."""

import sqlite3

CURRENT_SCHEMA = 2
MAX_READ_SCHEMA = 2
MIN_SCHEMA = 0
# Schema 1 did not record its policy. These are the largest bounds accepted by
# that release, not the defaults. Never infer an old grant's bound from new config.
LEGACY_FENCE_WINDOW_SECONDS = 300 + 600


def schema_version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def _layout(db):
    tables = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        name: [
            tuple(r)[1:] for r in db.execute('PRAGMA table_info("' + name.replace('"', '""') + '")')
        ]
        for name in tables
    }


def _expected(version=CURRENT_SCHEMA):
    with sqlite3.connect(":memory:") as db:
        _baseline(db)
        if version >= 2:
            _durable_fence_window(db)
        return _layout(db)


def validate_schema(db, version=None):
    version = schema_version(db) if version is None else version
    if version not in (1, 2):
        raise ValueError("journal_schema_unsupported")
    if schema_version(db) != version or _layout(db) != _expected(version):
        raise ValueError("journal_schema_invalid")
    for index in ("one_pending_switch", "assignments_device_protocol"):
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index,)
        ).fetchone()
        with sqlite3.connect(":memory:") as expected:
            _baseline(expected)
            wanted = expected.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index,)
            ).fetchone()[0]
        if not row or row[0] != wanted:
            raise ValueError("journal_schema_invalid")


def migrate(store):
    with store.db() as db:
        version = schema_version(db)
        if not MIN_SCHEMA <= version <= MAX_READ_SCHEMA:
            raise ValueError("journal_schema_unsupported")
        if version == 0:
            # Only known unversioned layouts are adopted. Do not stamp a foreign
            # database or repair a damaged versioned journal on application start.
            expected = _expected()
            for table, columns in _layout(db).items():
                if table not in expected or any(
                    column not in expected[table] for column in columns
                ):
                    raise ValueError("journal_legacy_schema_invalid")
            _baseline(db)
            _durable_fence_window(db)
            db.execute(f"PRAGMA user_version={CURRENT_SCHEMA}")
        elif version == 1:
            validate_schema(db, version=1)
            _durable_fence_window(db)
            db.execute(f"PRAGMA user_version={CURRENT_SCHEMA}")
        validate_schema(db)


def initialize_registry(store):
    # Kept as an internal compatibility entry point; migration creates all tables.
    migrate(store)


def _durable_fence_window(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(control_leases)")}
    if "fence_window_seconds" in columns:
        return
    db.execute(
        "ALTER TABLE control_leases ADD COLUMN fence_window_seconds "
        f"INTEGER NOT NULL DEFAULT {LEGACY_FENCE_WINDOW_SECONDS} "
        "CHECK(fence_window_seconds >= 245)"
    )
    # Start a conservative monotonic wait on first use of a migrated fence.
    # Do not trust an old deadline computed with an unrecorded policy.
    db.execute("UPDATE control_leases SET fence_boot='' WHERE fenced=1")


def _baseline(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS assignments (\n                external_id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE,\n                node_id TEXT NOT NULL, server_id TEXT NOT NULL,\n                creation_expires_at TEXT NOT NULL, remote_id TEXT,\n                created_at REAL NOT NULL, last_operation TEXT, last_outcome TEXT, checked_at REAL)"
    )
    # Additive migration: existing aliases, external IDs and remote peers stay intact.
    columns = {row[1] for row in db.execute("PRAGMA table_info(assignments)")}
    additions = {
        "device_id": "TEXT NOT NULL DEFAULT ''",
        "protocol": "TEXT NOT NULL DEFAULT 'amneziawg'",
        "configuration_revision": "INTEGER NOT NULL DEFAULT 0",
        "configuration_digest": "TEXT",
        "binding_key": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE assignments ADD COLUMN {name} {definition}")
    db.execute("UPDATE assignments SET device_id=external_id WHERE device_id=''")
    db.execute("UPDATE assignments SET binding_key=external_id WHERE binding_key=''")
    db.execute(
        "CREATE TABLE IF NOT EXISTS switches (client_id TEXT NOT NULL, operation_id TEXT NOT NULL, source_node TEXT NOT NULL, target_node TEXT NOT NULL, target_server TEXT NOT NULL, binding_key TEXT NOT NULL, name TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, disable_requested INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(client_id, operation_id))"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS one_pending_switch ON switches(client_id) WHERE state NOT IN ('complete', 'cancelled')"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS assignments_device_protocol ON assignments(device_id, protocol)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS control_leases (node_id TEXT PRIMARY KEY, server_id TEXT NOT NULL, controller TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0, valid_until REAL NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, fenced INTEGER NOT NULL DEFAULT 0, fence_boot TEXT NOT NULL DEFAULT '', fence_after REAL NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS access_cache (client_id TEXT PRIMARY KEY, binding_key TEXT NOT NULL, payload TEXT NOT NULL, denied INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS retired_bindings (node_id TEXT NOT NULL, server_id TEXT NOT NULL, remote_id TEXT NOT NULL, binding_key TEXT NOT NULL, cleaned INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(node_id,binding_key))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS node_registry (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, configuration TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS registry_meta (id INTEGER PRIMARY KEY CHECK(id=1), initialized INTEGER NOT NULL)"
    )
