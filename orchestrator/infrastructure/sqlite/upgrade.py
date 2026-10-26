"""Offline upgrade rehearsal on a disposable SQLite copy. Never contact nodes."""

import sqlite3
from pathlib import Path
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.sqlite.migrations import schema_version


def rows(directory):
    with sqlite3.connect(Path(directory) / "assignments.sqlite3") as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        records = {
            name: [
                dict(r)
                for r in db.execute(
                    'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'
                )
            ]
            for name in tables
        }
        return schema_version(db), records


def rehearse(directory):
    before_version, before = rows(directory)
    Assignments(directory)
    after_version, after = rows(directory)
    for table, records in before.items():
        if len(after.get(table, [])) != len(records):
            raise ValueError("migration_changed_existing_rows")
        for old, new in zip(records, after[table]):
            for key, value in old.items():
                if table == "assignments" and key in ("device_id", "binding_key") and value == "":
                    value = old["external_id"]  # The documented legacy identity backfill.
                if (
                    before_version in (0, 1)
                    and after_version == 2
                    and table == "control_leases"
                    and key == "fence_boot"
                    and old["fenced"] == 1
                    and "fence_window_seconds" not in old
                    and new.get("fence_window_seconds") == 900
                ):
                    # Schema 2 deliberately restarts old fences for the full
                    # conservative window. No other field or value may change.
                    value = ""
                if new.get(key) != value:
                    raise ValueError("migration_changed_existing_rows")
