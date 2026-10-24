"""Durable routing journal; contains no VPN keys or configurations."""

from orchestrator.application.execution import remaining, DeadlineExceeded
import os
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing, AbstractContextManager
from typing import Mapping, Sequence, Iterator, cast
from orchestrator.domain.records import Assignment
from orchestrator.domain.models import Observation
from orchestrator.domain.contracts import TransportConfiguration
from orchestrator.config.policy import RuntimePolicy
from pathlib import Path
from orchestrator.domain.models import OrchestratorError


class Assignments:
    def __init__(self, directory: str | Path, policy: RuntimePolicy | None = None) -> None:
        self.policy = policy or RuntimePolicy()
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError("Private state directory required")
        self.path = directory / "assignments.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise ValueError("Private database required")
        from orchestrator.infrastructure.sqlite.migrations import migrate

        migrate(self)
        from orchestrator.infrastructure.sqlite.switches import SwitchRepository
        from orchestrator.infrastructure.sqlite.leases import LeaseRepository
        from orchestrator.infrastructure.sqlite.inventory import InventoryRepository

        self.switches = SwitchRepository(self)
        self.leases = LeaseRepository(self)
        self.inventory = InventoryRepository(self)

    def lock(self, identity: str) -> AbstractContextManager[None]:
        from orchestrator.infrastructure.sqlite.locking import connection_lock

        return connection_lock(self, identity)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        # A remote mutation may have completed at the deadline. Still allow an
        # uncontended final journal write; never abandon it just due to the clock.
        try:
            available = remaining()
        except DeadlineExceeded:
            available = 0
        db = sqlite3.connect(self.path, timeout=min(10, available if available is not None else 10))
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def usage_bindings(self, row: Assignment) -> list[dict]:
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM retired_bindings WHERE binding_key=? OR binding_key IN (SELECT binding_key FROM switches WHERE client_id=?) ORDER BY node_id,binding_key LIMIT 101",
                    (row["external_id"], row["client_id"]),
                )
            ]

    def rows(self) -> list[Assignment]:
        with self.db() as db:
            return [
                cast(Assignment, dict(row))
                for row in db.execute("SELECT * FROM assignments ORDER BY created_at")
            ]

    def get(
        self, external_id: str | None = None, client_id: str | None = None
    ) -> Assignment | None:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM assignments WHERE external_id=?"
                if external_id is not None
                else "SELECT * FROM assignments WHERE client_id=?",
                (external_id if external_id is not None else client_id,),
            ).fetchone()
            return cast(Assignment, dict(row)) if row else None

    def reserve(
        self,
        payload: Mapping[str, str],
        observations: Sequence[Observation],
        device_id: str | None = None,
        protocol: str = "amneziawg",
    ) -> Assignment:
        device_id = device_id or payload["external_id"]
        with self.db() as db:
            existing = db.execute(
                "SELECT * FROM assignments WHERE external_id=?", (payload["external_id"],)
            ).fetchone()
            if existing:
                if existing["device_id"] != device_id or existing["protocol"] != protocol:
                    raise OrchestratorError("assignment_identity_conflict", 409)
                return cast(Assignment, dict(existing))
            if db.execute(
                "SELECT 1 FROM assignments WHERE device_id=? AND protocol=?", (device_id, protocol)
            ).fetchone():
                raise OrchestratorError("assignment_identity_conflict", 409)
            choices = []
            for observation in observations:
                if (
                    not 0
                    <= time.time() - observation.started_at
                    <= self.policy.selection.observation_max_age_seconds
                    or observation.node.mode != "active"
                    or observation.node.protocol != protocol
                ):
                    continue
                rows = db.execute(
                    "SELECT created_at FROM assignments WHERE node_id=?", (observation.node.id,)
                ).fetchall()
                pending = db.execute(
                    "SELECT created_at FROM switches WHERE target_node=? AND state NOT IN ('complete', 'cancelled')",
                    (observation.node.id,),
                ).fetchall()
                occupied = max(
                    len(rows) + len(pending),
                    observation.total_peers
                    + sum(row["created_at"] >= observation.started_at for row in [*rows, *pending]),
                )
                capacity = min(observation.node.capacity, observation.max_peers)
                if occupied < capacity:
                    choices.append(
                        (occupied / capacity, observation.node.id, observation.node.server_id)
                    )
            if not choices:
                raise OrchestratorError("no_eligible_node", 503)
            _, node_id, server_id = min(choices)
            db.execute(
                "INSERT INTO assignments (external_id, client_id, node_id, server_id, creation_expires_at, created_at, device_id, protocol, binding_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    payload["external_id"],
                    "wgapi_" + uuid.uuid4().hex,
                    node_id,
                    server_id,
                    payload["expires_at"],
                    time.time(),
                    device_id,
                    protocol,
                    payload["external_id"],
                ),
            )
            return cast(
                Assignment,
                dict(
                    db.execute(
                        "SELECT * FROM assignments WHERE external_id=?", (payload["external_id"],)
                    ).fetchone()
                ),
            )

    def bind(self, row: Assignment, remote_id: str) -> None:
        with self.db() as db:
            changed = db.execute(
                "UPDATE assignments SET remote_id=? WHERE client_id=? AND (remote_id IS NULL OR remote_id=?)",
                (remote_id, row["client_id"], remote_id),
            ).rowcount
            if changed != 1:
                raise OrchestratorError("remote_identity_conflict", 409)

    def record(self, row: Assignment, operation: str, outcome: str) -> None:
        with self.db() as db:
            db.execute(
                "UPDATE assignments SET last_operation=?, last_outcome=?, checked_at=? WHERE client_id=?",
                (operation, outcome, time.time(), row["client_id"]),
            )

    def for_device(self, device_id: str) -> list[Assignment]:
        with self.db() as db:
            return [
                cast(Assignment, dict(row))
                for row in db.execute(
                    "SELECT * FROM assignments WHERE device_id=? ORDER BY created_at", (device_id,)
                )
            ]

    def configuration_revision(
        self, row: Assignment, configuration: TransportConfiguration, expires_at: str
    ) -> int:
        # Persist only a digest and counter, never credentials or configuration bytes.
        serialized = json.dumps(
            {"configuration": configuration.model_dump(), "expires_at": expires_at},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(serialized).hexdigest()
        with self.db() as db:
            current = db.execute(
                "SELECT * FROM assignments WHERE client_id=?", (row["client_id"],)
            ).fetchone()
            if current is None:
                raise OrchestratorError("not_found", 404)
            revision = current["configuration_revision"]
            if current["configuration_digest"] != digest:
                if revision != row["configuration_revision"]:
                    raise OrchestratorError("configuration_changed", 409)
                revision += 1
                db.execute(
                    "UPDATE assignments SET configuration_digest=?, configuration_revision=? WHERE client_id=?",
                    (digest, revision, row["client_id"]),
                )
            return revision

    def diagnostic_counts(self) -> dict[str, int | float]:
        """Read local counts only; do not take a write reservation or contact nodes."""
        with closing(
            sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            creates = db.execute(
                "SELECT COUNT(*) FROM assignments WHERE remote_id IS NULL"
            ).fetchone()[0]
            switches, oldest = db.execute(
                "SELECT COUNT(*), MIN(created_at) FROM switches WHERE state NOT IN ('complete','cancelled')"
            ).fetchone()
        return {
            "pending_creates": creates,
            "pending_switches": switches,
            "oldest_pending_seconds": max(0, time.time() - oldest) if oldest is not None else 0,
        }
