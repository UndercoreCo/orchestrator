"""Durable node permission and fencing. Never infer revocation from an HTTP timeout."""

import json
from orchestrator.application.execution import bounded

from contextvars import copy_context
from orchestrator.application.telemetry import Stage, observed, span
import time
import uuid
from datetime import datetime, timezone
from orchestrator.domain.records import LeaseGrant
from orchestrator.domain.models import OrchestratorError

LEASE_SECONDS = 90
# Includes the node runtime's independent 90-second watchdog and bounded clock skew.
FENCE_GRACE_SECONDS = 125
MAX_NODE_LEASE_SECONDS = 120
# Linux monotonic time is comparable between API/worker processes in the same boot.
# On other systems a process restart conservatively starts the wait again.


def timestamp(seconds):
    return (
        datetime.fromtimestamp(seconds, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def seconds(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class NodeLeases:
    def __init__(self, gateway):
        self.gateway, self.store = gateway, gateway.store

    def state(self, node):
        row = self.store.leases.state(node.id)
        if row and row["server_id"] != node.server_id:
            raise OrchestratorError("lease_node_identity_changed", 503)
        return row

    def ready(self, node):
        if not node.lease_enabled:
            return True
        state = self.state(node)
        return bool(
            state
            and state["verified"]
            and not state["fenced"]
            and state["valid_until"] > time.time() + 10
        )

    @bounded
    def heartbeat(self, node):
        if not node.lease_enabled:
            return
        with self.store.lock("node-" + node.id):
            state = self.state(node)
            if state and state["fenced"]:
                return
            self.renew_locked(node)

    def renew_locked(self, node):
        transport = self.gateway.lease_transports.for_node(node)
        lease = transport.observe(node)
        if abs(lease.server_time - time.time()) > 5:
            raise OrchestratorError("control_lease_unavailable", 503)
        row = self.store.leases.state(node.id)
        controller = row["controller"] if row else "ctl_" + uuid.uuid4().hex
        sequence = (row["sequence"] if row else 0) + 1
        if lease.required and lease.controller_id != controller:
            raise OrchestratorError("controller_conflict", 409)
        if lease.required and lease.sequence >= sequence:
            raise OrchestratorError("lease_sequence_conflict", 409)
        deadline = time.time() + self.gateway.policy.leases.duration_seconds
        # Caller holds the node lock across reservation and HTTP. Persist the
        # upper bound BEFORE sending; an uncertain response must not shorten it.
        policy = self.gateway.policy.leases
        self.store.leases.reserve(
            node.id,
            node.server_id,
            controller,
            sequence,
            deadline,
            policy.max_node_lease_seconds + policy.fence_grace_seconds,
        )
        transport.renew(node, LeaseGrant(controller, sequence, timestamp(deadline)))
        self.store.leases.verify(node.id)

    def fence(self, node):
        with self.store.lock("node-" + node.id):
            state = self.state(node)
            if not node.lease_enabled or not state or not state["verified"]:
                raise OrchestratorError("revoke_unconfirmed", 503)
            if not state["fenced"] or state["fence_boot"] != self.gateway.boot_id:
                window = max(
                    state["fence_window_seconds"],
                    self.gateway.policy.leases.max_node_lease_seconds
                    + self.gateway.policy.leases.fence_grace_seconds,
                )
                self.store.leases.fence(
                    node.id,
                    self.gateway.boot_id,
                    time.monotonic() + window,
                    window,
                )

    def expired(self, node):
        state = self.state(node)
        if not (node.lease_enabled and state and state["verified"] and state["fenced"]):
            return False
        if state["fence_boot"] != self.gateway.boot_id:
            self.fence(node)  # Reboot/unknown clock epoch: wait the full bound again.
            return False
        return time.monotonic() >= state["fence_after"]

    def remember(self, row, data):
        self.store.leases.remember(row, data)

    def deny(self, row, value=True):
        self.store.leases.deny(row, value)

    def denied(self, row):
        saved = self.store.leases.cached(row["client_id"])
        return bool(saved and saved["denied"])

    def cached_source(self, row):
        node = self.gateway.node(row)
        saved = self.store.leases.cached(row["client_id"])
        if (
            not saved
            or saved["denied"]
            or saved["binding_key"] != row["binding_key"]
            or not row["remote_id"]
        ):
            raise OrchestratorError("offline_grant_unavailable", 503)
        data = json.loads(saved["payload"])
        if data.get("status") != "active" or seconds(data["expires_at"]) <= time.time():
            raise OrchestratorError("access_unavailable", 410)
        state = self.state(node)
        if not node.lease_enabled or not state or not state["verified"]:
            raise OrchestratorError("offline_grant_unavailable", 503)
        return data

    def disabled_view(self, row):
        node = self.gateway.node(row)
        self.fence(node)
        self.retire(row)
        saved = self.store.leases.cached(row["client_id"])
        if not saved or saved["binding_key"] != row["binding_key"]:
            raise OrchestratorError("offline_grant_unavailable", 503)
        data = json.loads(saved["payload"])
        if not {"name", "expires_at", "created_at", "updated_at"} <= data.keys():
            raise OrchestratorError("offline_grant_unavailable", 503)
        return {
            **data,
            "client_id": row["client_id"],
            "external_id": row["external_id"],
            "status": "disabled",
        }

    def retire(self, row):
        node = self.gateway.node(row)
        if not self.expired(node):
            raise OrchestratorError("lease_waiting", 503)
        self.store.leases.retire(row)

    @bounded
    def restore(self, node):
        """Operator-only: revoke stale bindings BEFORE opening the old node again."""
        with self.store.lock("node-" + node.id):
            pending, stale, denied = self.store.leases.restore_snapshot(node.id)
            if pending:
                raise OrchestratorError("node_has_pending_switches", 409)
            driver = self.gateway.drivers.for_node(node)
            for row in [*stale, *denied]:
                if row["server_id"] != node.server_id or not row["remote_id"]:
                    raise OrchestratorError("node_identity_invalid", 503)
                data = driver.validate(
                    driver.mutate(node, row["remote_id"], "disable", {}),
                    external_id=row["binding_key"],
                    client_id=row["remote_id"],
                )
                if data["status"] not in ("disabled", "expired"):
                    raise OrchestratorError("revoke_unconfirmed", 503)
                self.store.leases.cleaned(node.id, row["binding_key"])
            self.renew_locked(node)
            self.store.leases.unfence(node.id)


@observed(Stage.HEARTBEAT)
def heartbeat_all(gateway):
    from concurrent.futures import ThreadPoolExecutor

    nodes = [n for n in gateway.nodes.values() if n.lease_enabled]

    def one(node):
        try:
            with span(Stage.HEARTBEAT, node_id=node.id):
                NodeLeases(gateway).heartbeat(node)
            return True
        except Exception:
            return False  # Counts only, no response bodies or tokens.

    with ThreadPoolExecutor(max_workers=gateway.policy.leases.heartbeat_workers) as pool:
        futures = [pool.submit(copy_context().run, one, node) for node in nodes]
        results = [future.result() for future in futures]
    return {"checked": len(results), "failed": results.count(False)}
