"""Switch journal. Publishing a target and completing its operation is atomic."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.infrastructure.sqlite.assignments import Assignments


import time
import uuid
from typing import cast, Sequence
from orchestrator.domain.contracts import SwitchRequest
from orchestrator.domain.models import Observation
from orchestrator.domain.records import NodeAccess
from orchestrator.domain.models import OrchestratorError
from orchestrator.domain.records import SwitchOperation, Assignment


class SwitchRepository:
    def __init__(self, store: Assignments) -> None:
        self.store = store

    def pending(self, client_id: str) -> SwitchOperation | None:
        with self.store.db() as db:
            row = db.execute(
                "SELECT * FROM switches WHERE client_id=? AND state NOT IN ('complete', 'cancelled')",
                (client_id,),
            ).fetchone()
            return cast(SwitchOperation, dict(row)) if row else None

    def history(
        self, client_id: str, operation_id: str
    ) -> tuple[SwitchOperation | None, float | None]:
        with self.store.db() as db:
            row = db.execute(
                "SELECT * FROM switches WHERE client_id=? AND operation_id=?",
                (client_id, operation_id),
            ).fetchone()
            latest = db.execute(
                "SELECT MAX(created_at) FROM switches WHERE client_id=?", (client_id,)
            ).fetchone()[0]
            return (cast(SwitchOperation, dict(row)) if row else None), latest

    def queued(self, limit: int) -> list[str]:
        with self.store.db() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT s.client_id FROM switches s JOIN assignments a USING(client_id) WHERE s.state NOT IN ('complete','cancelled') ORDER BY COALESCE(a.checked_at,0), s.created_at LIMIT ?",
                    (limit,),
                )
            ]

    def touch(self, client_id: str) -> None:
        with self.store.db() as db:
            db.execute(
                "UPDATE assignments SET checked_at=? WHERE client_id=?", (time.time(), client_id)
            )

    def state(self, operation: SwitchOperation, state: str) -> None:
        with self.store.db() as db:
            db.execute(
                "UPDATE switches SET state=? WHERE client_id=? AND operation_id=?",
                (state, operation["client_id"], operation["operation_id"]),
            )

    def request_disable(self, operation: SwitchOperation) -> None:
        with self.store.db() as db:
            db.execute(
                "UPDATE switches SET disable_requested=1 WHERE client_id=? AND operation_id=?",
                (operation["client_id"], operation["operation_id"]),
            )

    def complete(self, row: Assignment, operation: SwitchOperation, remote_id: str) -> None:
        with self.store.db() as db:
            # Retain routing identity for readback of final node measurements. No counters here.
            db.execute(
                "INSERT OR IGNORE INTO retired_bindings (node_id,server_id,remote_id,binding_key,cleaned) VALUES (?,?,?,?,1)",
                (row["node_id"], row["server_id"], row["remote_id"], row["binding_key"]),
            )
            db.execute(
                "UPDATE assignments SET node_id=?, server_id=?, remote_id=?, binding_key=?, last_operation='switch', last_outcome='ok', checked_at=? WHERE client_id=?",
                (
                    operation["target_node"],
                    operation["target_server"],
                    remote_id,
                    operation["binding_key"],
                    time.time(),
                    row["client_id"],
                ),
            )
            db.execute(
                "UPDATE switches SET state='complete' WHERE client_id=? AND operation_id=?",
                (row["client_id"], operation["operation_id"]),
            )

    def reserve(
        self,
        row: Assignment,
        request: SwitchRequest,
        source: NodeAccess,
        observations: Sequence[Observation],
    ) -> SwitchOperation:
        with self.store.db() as db:
            choices = []
            for observation in observations:
                if (
                    not 0
                    <= time.time() - observation.started_at
                    <= self.store.policy.selection.observation_max_age_seconds
                ):
                    continue
                node = observation.node
                assigned = db.execute(
                    "SELECT created_at FROM assignments WHERE node_id=?", (node.id,)
                ).fetchall()
                pending = db.execute(
                    "SELECT created_at FROM switches WHERE target_node=? AND state NOT IN ('complete', 'cancelled')",
                    (node.id,),
                ).fetchall()
                occupied = max(
                    len(assigned) + len(pending),
                    observation.total_peers
                    + sum(r["created_at"] >= observation.started_at for r in [*assigned, *pending]),
                )
                capacity = min(node.capacity, observation.max_peers)
                if occupied < capacity:
                    choices.append((occupied / capacity, node.id, node.server_id))
            if not choices:
                raise OrchestratorError("no_alternative_node", 503)
            _, target, identity = min(choices)
            value = (
                row["client_id"],
                request.idempotency_key,
                row["node_id"],
                target,
                identity,
                "switch_" + uuid.uuid4().hex,
                source["name"],
                source["expires_at"],
                "reserved",
                time.time(),
            )
            db.execute(
                "INSERT INTO switches (client_id,operation_id,source_node,target_node,target_server,binding_key,name,expires_at,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                value,
            )
            return cast(
                SwitchOperation,
                dict(
                    db.execute(
                        "SELECT * FROM switches WHERE client_id=? AND operation_id=?", value[:2]
                    ).fetchone()
                ),
            )
