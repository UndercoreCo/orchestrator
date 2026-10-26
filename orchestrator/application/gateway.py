"""Internal VpnGateway-compatible facade. The calling backend remains the entitlement authority."""

from orchestrator.application.execution import bounded, remaining

from orchestrator.application.repository_ports import Journal
from orchestrator.application.ports import Resources
from orchestrator.application.drivers import DriverRegistry, LeaseRegistry
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from orchestrator.application.telemetry import NullObserver, Stage, observed, span
from orchestrator.domain.models import OrchestratorError
from orchestrator.application.ports import NodeGrant, NodeConnection


class AgentGateway:
    def __init__(
        self,
        nodes,
        store: Journal,
        drivers: DriverRegistry,
        lease_transports: LeaseRegistry,
        resources: Resources,
        boot_id: str,
        policy=None,
        telemetry=None,
    ):
        from orchestrator.config.policy import RuntimePolicy

        self.policy = policy or RuntimePolicy()
        self.telemetry = telemetry if telemetry is not None else NullObserver()
        if any(
            len(set(values)) != len(values)
            for values in (
                [n.id for n in nodes],
                [n.server_id for n in nodes],
                [n.api_url.lower().rstrip("/") for n in nodes],
            )
        ):
            raise ValueError("Duplicate node")
        self._nodes = {n.id: n for n in nodes}
        self.registry = None
        self.store, self.resources = store, resources
        self.lease_transports = lease_transports
        self.boot_id = boot_id
        self.drivers = drivers
        for node in nodes:
            self.drivers.for_node(node)

    def close(self):
        self.resources.close()

    @property
    def nodes(self):
        return self.registry.nodes() if self.registry else self._nodes

    def available(self, node):
        from orchestrator.application.leases import NodeLeases

        return node.mode == "active" and NodeLeases(self).ready(node)

    def node(self, row, issuance=False):
        node = self.nodes.get(row["node_id"])
        if node is None or node.server_id != row["server_id"] or node.protocol != row["protocol"]:
            raise OrchestratorError("assigned_node_identity_changed", 503)
        if issuance:
            from orchestrator.application.leases import NodeLeases

            if not NodeLeases(self).ready(node):
                raise OrchestratorError("node_permission_unavailable", 503)
        if issuance and node.mode == "disabled":
            raise OrchestratorError("assigned_node_disabled", 503)
        return node

    @observed(
        Stage.CREATE,
        identity=lambda self, payload, **kw: (
            kw.get("device_id") or payload["external_id"],
            "create",
        ),
    )
    @bounded
    def create(self, payload, *, protocol="amneziawg", device_id=None):
        if datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")) <= datetime.now(
            timezone.utc
        ):
            raise OrchestratorError("grant_expired", 410)
        self.drivers.for_protocol(protocol)
        device_id = device_id or payload["external_id"]
        row = self.store.get(external_id=payload["external_id"])
        if row is not None and (row["protocol"] != protocol or row["device_id"] != device_id):
            raise OrchestratorError("assignment_identity_conflict", 409)
        if row is None:
            # Independent I/O, bounded fan-out. Reservation below still serializes
            # capacity decisions, including assignments created during observation.
            nodes = [n for n in self.nodes.values() if self.available(n) and n.protocol == protocol]

            def observe(node):
                try:
                    return self.drivers.for_node(node).observe(node)
                except OrchestratorError:
                    return None

            with span(Stage.SELECT):
                with ThreadPoolExecutor(
                    max_workers=self.policy.selection.observation_workers
                ) as pool:
                    futures = [pool.submit(copy_context().run, observe, node) for node in nodes]
                    observations = [
                        value for future in futures if (value := future.result()) is not None
                    ]
                remaining()
                row = self.store.reserve(payload, observations, device_id, protocol)
        with self.store.lock(row["client_id"]):
            return self.create_reserved(self.store.get(client_id=row["client_id"]), payload)

    def create_reserved(self, row, payload):
        self.require_settled(row)
        if row["creation_expires_at"] != payload["expires_at"]:
            raise OrchestratorError("creation_lifetime_conflict", 409)
        node = self.node(row, issuance=True)
        if row["binding_key"] != row["external_id"]:
            return self.client(row["client_id"])
        # Retrying is safe ONLY with the durable Undercore agent, using the SAME
        # external_id + creation expiry on the SAME node, even after response loss.
        grant = NodeGrant(payload["external_id"], payload["name"], payload["expires_at"])
        return self.invoke(row, "create", lambda: self.drivers.for_node(node).create(node, grant))

    def resolve(self, row):
        if row["remote_id"]:
            return row["remote_id"]
        node = self.node(row)
        matches = [
            c
            for c in self.drivers.for_node(node).list(node)
            if c["external_id"] == row["binding_key"]
        ]
        if len(matches) > 1:
            raise OrchestratorError("remote_identity_conflict", 409)
        if not matches:
            raise OrchestratorError("not_found", 404)
        self.store.bind(row, matches[0]["client_id"])
        return matches[0]["client_id"]

    @observed(Stage.MUTATE)
    def invoke(self, row, operation, call):
        try:
            data = self.drivers.for_node(self.node(row)).validate(
                call(), external_id=row["binding_key"], client_id=row["remote_id"]
            )
            self.store.bind(row, data["client_id"])
            self.store.record(row, operation, "ok")
            from orchestrator.application.leases import NodeLeases

            NodeLeases(self).remember(row, data)
            if operation == "enable":
                NodeLeases(self).deny(row, False)
            return {**data, "client_id": row["client_id"], "external_id": row["external_id"]}
        except OrchestratorError as error:
            self.store.record(row, operation, error.code)
            raise

    def require_settled(self, row):
        from orchestrator.application.switches import Switches

        if Switches(self).pending(row["client_id"]):
            raise OrchestratorError("switch_in_progress", 503)

    @observed(
        Stage.ACCESS,
        identity=lambda self, client_id, operation="get", payload=None: (
            client_id,
            operation,
            (payload or {}).get("idempotency_key", ""),
        ),
    )
    @bounded
    def client(self, client_id, operation="get", payload=None):
        with self.store.lock(client_id):
            try:
                return self.client_locked(client_id, operation, payload)
            except OrchestratorError as error:
                if operation != "disable" or error.status != 503:
                    raise
                from orchestrator.application.leases import NodeLeases

                row = self.store.get(client_id=client_id)
                if not row or not self.node(row).lease_enabled:
                    raise
                return NodeLeases(self).disabled_view(row)

    def client_locked(self, client_id, operation="get", payload=None):
        row = self.store.get(client_id=client_id)
        if row is None:
            raise OrchestratorError("not_found", 404)
        from orchestrator.application.leases import NodeLeases

        if operation == "disable":
            NodeLeases(self).deny(row)
        from orchestrator.application.switches import Switches

        pending = Switches(self).pending(client_id)
        if pending and operation == "disable":
            Switches(self).disable_pending(row, pending)
            row = self.store.get(client_id=client_id)
        self.require_settled(row)
        if (
            operation == "replace"
            and (payload or {}).get("expected_external_id") == row["external_id"]
        ):
            payload = {**payload, "expected_external_id": row["binding_key"]}
        issue = operation in ("configuration", "amnezia", "enable") or (
            operation == "replace" and (payload or {}).get("allow_create", True)
        )
        node = self.node(row, issuance=issue)
        remote_id = self.resolve(row)
        row = {**row, "remote_id": remote_id}
        driver = self.drivers.for_node(node)
        if operation in ("configuration", "amnezia"):
            if row["protocol"] != "amneziawg":
                raise OrchestratorError("unsupported_format", 422)
            return driver.export(
                node, remote_id, {"configuration": "conf", "amnezia": "amnezia-vpn"}[operation]
            ).data
        return self.invoke(
            row,
            operation,
            lambda: driver.get(node, remote_id)
            if operation == "get"
            else driver.mutate(node, remote_id, operation, payload or {}),
        )

    @observed(Stage.CONFIGURATION, identity=lambda self, client_id: (client_id, "configuration"))
    @bounded
    def connection(self, client_id):
        """Read state and transport parameters under the same assignment lock."""
        with self.store.lock(client_id):
            row = self.store.get(client_id=client_id)
            if row is None:
                raise OrchestratorError("not_found", 404)
            self.require_settled(row)
            node = self.node(row, issuance=True)
            row = {**row, "remote_id": self.resolve(row)}
            snapshot = None

            def fetch():
                nonlocal snapshot
                snapshot = self.drivers.for_node(node).connection(
                    node, row["remote_id"], row["binding_key"]
                )
                return snapshot.client

            value = self.invoke(row, "configuration", fetch)
            return NodeConnection(value, snapshot.configuration)

    @bounded
    def lookup(self, external_id):
        """Indexed identity lookup; missing assignments never contact VPN nodes."""
        row = self.store.get(external_id=external_id)
        if row is None:
            return {"clients": []}
        if row["protocol"] != "amneziawg":
            raise OrchestratorError("assignment_identity_conflict", 409)
        try:
            client = self.client(row["client_id"])
        except OrchestratorError as error:
            if error.status != 404:
                raise
            # A confirmed missing peer may be recovered by the same durable
            # creation intent. Timeouts and identity conflicts are never absence.
            return {"clients": []}
        return {"clients": [client]}

    @bounded
    def listing(self):
        result = []
        for row in self.store.rows():
            if row["protocol"] != "amneziawg":
                continue  # v1 clients only understand the legacy AWG contract.
            try:
                result.append(self.client(row["client_id"]))
            except OrchestratorError as error:
                if error.status != 404:
                    raise
        return {"clients": result}

    def overview(self):
        rows = self.store.rows()
        result = []
        for node in self.nodes.values():
            assigned = [r for r in rows if r["node_id"] == node.id]
            last = max(assigned, key=lambda r: r["checked_at"] or 0, default=None)
            result.append(
                {
                    "id": node.id,
                    "protocol": node.protocol,
                    "region": node.region,
                    "mode": node.mode,
                    "assigned": len(assigned),
                    "capacity": node.capacity,
                    "pending": sum(r["remote_id"] is None for r in assigned),
                    "last_operation": last["last_operation"] if last else None,
                    "last_outcome": last["last_outcome"] if last else None,
                    "checked_at": last["checked_at"] if last else None,
                }
            )
        return {"nodes": result}
