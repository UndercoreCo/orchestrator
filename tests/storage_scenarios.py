"""Explicit test-only simulation of the planned schema-2 producer release."""

from orchestrator.infrastructure.sqlite.migrations import _durable_fence_window, validate_schema


def promote_schema_two(store):
    with store.db() as db:
        _durable_fence_window(db)
        db.execute("PRAGMA user_version=2")
        validate_schema(db)
