"""Lease journal transactions; callers retain the per-node/assignment lock."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.infrastructure.sqlite.assignments import Assignments


import json
from typing import cast, Mapping
from orchestrator.domain.records import LeaseState, CachedAccess, RetiredBinding, Assignment
from orchestrator.infrastructure.sqlite.migrations import (
    LEGACY_FENCE_WINDOW_SECONDS,
    schema_version,
)

# A schema-1 deadline from older code may have used only 245 seconds. Mark new
# 900-second fences in an existing column without changing the legacy layout.
LEGACY_FENCE_BOOT_PREFIX = "window900:"


class LeaseRepository:
    def __init__(self, store: Assignments) -> None:
        self.store = store

    def state(self, node_id: str) -> LeaseState | None:
        with self.store.db() as db:
            row = db.execute("SELECT * FROM control_leases WHERE node_id=?", (node_id,)).fetchone()
            if not row:
                return None
            state = dict(row)
            if schema_version(db) == 1:
                state["fence_window_seconds"] = LEGACY_FENCE_WINDOW_SECONDS
                boot = state["fence_boot"]
                state["fence_boot"] = (
                    boot.removeprefix(LEGACY_FENCE_BOOT_PREFIX)
                    if boot.startswith(LEGACY_FENCE_BOOT_PREFIX)
                    else ""
                )
            return cast(LeaseState, state)

    def reserve(
        self,
        node_id: str,
        server_id: str,
        controller: str,
        sequence: int,
        deadline: float,
        fence_window_seconds: int,
    ) -> None:
        with self.store.db() as db:
            if schema_version(db) == 1:
                if fence_window_seconds > LEGACY_FENCE_WINDOW_SECONDS:
                    raise ValueError("lease_window_requires_schema_upgrade")
                db.execute(
                    "INSERT INTO control_leases (node_id,server_id,controller,sequence,valid_until) VALUES (?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET sequence=excluded.sequence,valid_until=MAX(valid_until,excluded.valid_until)",
                    (node_id, server_id, controller, sequence, deadline),
                )
                return
            db.execute(
                "INSERT INTO control_leases (node_id,server_id,controller,sequence,valid_until,fence_window_seconds) VALUES (?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET sequence=excluded.sequence,valid_until=MAX(valid_until,excluded.valid_until),fence_window_seconds=MAX(fence_window_seconds,excluded.fence_window_seconds)",
                (node_id, server_id, controller, sequence, deadline, fence_window_seconds),
            )

    def verify(self, node_id: str) -> None:
        with self.store.db() as db:
            db.execute("UPDATE control_leases SET verified=1 WHERE node_id=?", (node_id,))

    def fence(self, node_id: str, boot: str, deadline: float, window_seconds: int) -> None:
        with self.store.db() as db:
            if schema_version(db) == 1:
                # Legacy callers must use the whole conservative window. A new
                # larger permission cannot be represented safely by this schema.
                if window_seconds != LEGACY_FENCE_WINDOW_SECONDS:
                    raise ValueError("legacy_fence_window_invalid")
                db.execute(
                    "UPDATE control_leases SET fenced=1,fence_boot=?,fence_after=? WHERE node_id=?",
                    (LEGACY_FENCE_BOOT_PREFIX + boot, deadline, node_id),
                )
                return
            db.execute(
                "UPDATE control_leases SET fenced=1,fence_boot=?,fence_after=?,fence_window_seconds=MAX(fence_window_seconds,?) WHERE node_id=?",
                (boot, deadline, window_seconds, node_id),
            )

    def remember(self, row: Assignment, data: Mapping[str, object]) -> None:
        # This whitelist is the persistence boundary, not merely a logging filter.
        safe = {
            k: data[k]
            for k in (
                "client_id",
                "external_id",
                "device_id",
                "name",
                "expires_at",
                "status",
                "created_at",
                "updated_at",
            )
            if k in data
        }
        with self.store.db() as db:
            db.execute(
                "INSERT INTO access_cache (client_id,binding_key,payload) VALUES (?,?,?) ON CONFLICT(client_id) DO UPDATE SET binding_key=excluded.binding_key,payload=excluded.payload",
                (row["client_id"], row["binding_key"], json.dumps(safe)),
            )

    def deny(self, row: Assignment, value: bool) -> None:
        with self.store.db() as db:
            db.execute(
                "INSERT INTO access_cache (client_id,binding_key,payload,denied) VALUES (?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET denied=excluded.denied",
                (row["client_id"], row["binding_key"], "{}", int(value)),
            )

    def cached(self, client_id: str) -> CachedAccess | None:
        with self.store.db() as db:
            row = db.execute(
                "SELECT * FROM access_cache WHERE client_id=?", (client_id,)
            ).fetchone()
            return cast(CachedAccess, dict(row)) if row else None

    def retire(self, row: Assignment) -> None:
        with self.store.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO retired_bindings (node_id,server_id,remote_id,binding_key) VALUES (?,?,?,?)",
                (row["node_id"], row["server_id"], row["remote_id"], row["binding_key"]),
            )

    def restore_snapshot(self, node_id: str) -> tuple[bool, list[RetiredBinding], list[Assignment]]:
        # Pending check and both lists must describe one transaction snapshot.
        with self.store.db() as db:
            pending = db.execute(
                "SELECT 1 FROM switches WHERE source_node=? AND state NOT IN ('complete','cancelled')",
                (node_id,),
            ).fetchone()
            stale = [
                cast(RetiredBinding, dict(r))
                for r in db.execute(
                    "SELECT * FROM retired_bindings WHERE node_id=? AND cleaned=0", (node_id,)
                )
            ]
            denied = [
                cast(Assignment, dict(r))
                for r in db.execute(
                    "SELECT a.* FROM assignments a JOIN access_cache c USING(client_id) WHERE a.node_id=? AND c.denied=1",
                    (node_id,),
                )
            ]
            return bool(pending), stale, denied

    def cleaned(self, node_id: str, binding_key: str) -> None:
        with self.store.db() as db:
            db.execute(
                "UPDATE retired_bindings SET cleaned=1 WHERE node_id=? AND binding_key=?",
                (node_id, binding_key),
            )

    def unfence(self, node_id: str) -> None:
        with self.store.db() as db:
            db.execute("UPDATE control_leases SET fenced=0 WHERE node_id=?", (node_id,))
