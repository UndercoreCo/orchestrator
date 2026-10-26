"""Pinned bridge runtime for compatibility tests; never installed in releases."""

import hashlib
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from orchestrator.infrastructure.sqlite.migrations import _durable_fence_window, validate_schema

BRIDGE = Path(__file__).parent / "fixtures/storage_bridge"


@contextmanager
def bridge_runtime():
    provenance = json.loads((BRIDGE / "provenance.json").read_text())
    with patch.dict(sys.modules):
        for short, name in (
            ("migrations", "orchestrator.infrastructure.sqlite.migrations"),
            ("leases", "orchestrator.infrastructure.sqlite.leases"),
            ("application_leases", "orchestrator.application.leases"),
        ):
            path = BRIDGE / (short + ".py")
            assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance["sha256"][path.name]
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        yield


def bridge_manifest():
    return json.loads((BRIDGE / "storage.json").read_text())


def promote_schema_two(store):
    with store.db() as db:
        _durable_fence_window(db)
        db.execute("PRAGMA user_version=2")
        validate_schema(db)
