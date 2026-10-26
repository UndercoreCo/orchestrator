"""Read-only SQLite snapshot; does not initialize/migrate storage or contact agents."""

import sqlite3
from contextlib import closing
from typing import Literal
from pathlib import Path
from datetime import datetime, timezone
from orchestrator.domain.inventory import NodeSettings
from orchestrator.domain.panel import PanelNode, PanelAssignment, PanelOverview
from orchestrator.infrastructure.sqlite.migrations import validate_schema

PAGE_SIZE = 50


class PanelJournal:
    def __init__(self, directory):
        self.path = Path(directory) / "assignments.sqlite3"

    def overview(self, node_offset: int, connection_offset: int) -> PanelOverview:
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            try:
                validate_schema(db)
            except ValueError:
                raise ValueError("panel_schema_unsupported")

            def count(sql):
                return db.execute(sql).fetchone()[0]

            nodes_total = count("SELECT COUNT(*) FROM node_registry")
            connections_total = count("SELECT COUNT(*) FROM assignments")
            pending_creates = count("SELECT COUNT(*) FROM assignments WHERE remote_id IS NULL")
            pending_switches = count(
                "SELECT COUNT(*) FROM switches WHERE state NOT IN ('complete','cancelled')"
            )
            denied = count("SELECT COUNT(*) FROM access_cache WHERE denied=1")
            nodes = []
            for row in db.execute(
                "SELECT r.configuration,r.revision,l.verified,l.fenced,l.valid_until, (SELECT COUNT(*) FROM assignments a WHERE a.node_id=r.id) AS assigned, (SELECT COUNT(*) FROM switches s WHERE s.target_node=r.id AND s.state NOT IN ('complete','cancelled')) AS pending FROM node_registry r LEFT JOIN control_leases l ON l.node_id=r.id ORDER BY r.id LIMIT ? OFFSET ?",
                (PAGE_SIZE, node_offset),
            ):
                n = NodeSettings.model_validate_json(row["configuration"])
                nodes.append(
                    PanelNode(
                        id=n.id,
                        protocol=n.protocol,
                        region=n.region,
                        mode=n.mode,
                        capacity=n.capacity,
                        revision=row["revision"],
                        assignments=row["assigned"],
                        pending_targets=row["pending"],
                        lease_enabled=n.lease_enabled,
                        lease_verified=bool(row["verified"]),
                        fenced=bool(row["fenced"]),
                        lease_valid_until=row["valid_until"],
                    )
                )
            assignments = []
            for row in db.execute(
                "SELECT a.client_id,a.node_id,a.protocol,a.remote_id,a.configuration_revision,a.checked_at,a.last_operation,a.last_outcome,c.denied,EXISTS(SELECT 1 FROM switches s WHERE s.client_id=a.client_id AND s.state NOT IN ('complete','cancelled')) AS pending FROM assignments a LEFT JOIN access_cache c USING(client_id) ORDER BY a.created_at DESC,a.client_id LIMIT ? OFFSET ?",
                (PAGE_SIZE, connection_offset),
            ):
                state: Literal["assigned", "pending_create", "pending_switch", "denied"] = (
                    "denied"
                    if row["denied"]
                    else "pending_switch"
                    if row["pending"]
                    else "pending_create"
                    if row["remote_id"] is None
                    else "assigned"
                )
                operation = (
                    row["last_operation"]
                    if row["last_operation"]
                    in {
                        "create",
                        "get",
                        "lookup",
                        "renew",
                        "replace",
                        "enable",
                        "disable",
                        "switch",
                        "configuration",
                        "amnezia",
                        "recover",
                    }
                    else None
                )
                outcome: Literal["ok", "error"] | None = (
                    None
                    if row["last_outcome"] is None
                    else "ok"
                    if row["last_outcome"] == "ok"
                    else "error"
                )
                assignments.append(
                    PanelAssignment(
                        connection_id=row["client_id"],
                        node_id=row["node_id"],
                        protocol=row["protocol"],
                        state=state,
                        revision=row["configuration_revision"],
                        checked_at=row["checked_at"],
                        last_operation=operation,
                        last_outcome=outcome,
                    )
                )
            return PanelOverview(
                generated_at=datetime.now(timezone.utc),
                nodes_total=nodes_total,
                connections_total=connections_total,
                pending_creates=pending_creates,
                pending_switches=pending_switches,
                denied=denied,
                node_offset=node_offset,
                connection_offset=connection_offset,
                page_size=PAGE_SIZE,
                nodes=nodes,
                connections=assignments,
            )
