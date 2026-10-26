"""Protocol-neutral internal service. The calling backend grants the slot; we route its access."""

from orchestrator.application.execution import bounded

from datetime import datetime, timezone
from orchestrator.application.telemetry import Stage, observed
from orchestrator.domain.contracts import Connection, ConnectionConfiguration, ConnectionExport
from orchestrator.domain.models import OrchestratorError


class Connections:
    def __init__(self, gateway):
        self.gateway = gateway

    def capabilities(self):
        # Registry only contains implemented drivers; configuring TrustTunnel alone
        # cannot advertise support. No credentials, origins or remote IDs in discovery.
        return {
            "schema_version": 1,
            "protocols": [
                {
                    "protocol": protocol,
                    "configuration_version": driver.capabilities.configuration_version,
                    "idempotent_create": driver.capabilities.idempotent_create,
                    "enforces_expiry": driver.capabilities.enforces_expiry,
                    "confirmed_revoke": driver.capabilities.confirmed_revoke,
                    "export_formats": list(driver.capabilities.export_formats),
                }
                for protocol, driver in sorted(self.gateway.drivers.drivers.items())
                if any(n.protocol == protocol for n in self.gateway.nodes.values())
            ],
        }

    @bounded
    def create(self, request):
        try:
            future = datetime.fromisoformat(request.expires_at.replace("Z", "+00:00"))
            if future.year > 2100 or future <= datetime.now(timezone.utc):
                raise ValueError()
        except ValueError:
            raise OrchestratorError("grant_expired", 410) from None
        rows = self.gateway.store.for_device(request.device_id)
        if rows:
            # Lost response/relaunch/capability change must never provision a second
            # access or move an uncertain assignment. Migration is a separate action.
            compatible = [
                r
                for r in rows
                if self.gateway.drivers.compatible(r["protocol"], request.capabilities)
            ]
            if not compatible:
                raise OrchestratorError("client_upgrade_required", 409)
            row = compatible[0]
            protocol, external_id = row["protocol"], row["external_id"]
        else:
            # Server policy, not client offer ordering: AWG remains preferred in this phase.
            protocols = sorted(
                {n.protocol for n in self.gateway.nodes.values() if n.mode == "active"}
            )
            compatible = [
                p for p in protocols if self.gateway.drivers.compatible(p, request.capabilities)
            ]
            if not compatible:
                raise OrchestratorError("no_compatible_protocol", 422)
            protocol = compatible[0]
            # Only one initial binding. Future explicit failover can create another
            # binding key while retaining the canonical device_id and single paid slot.
            external_id = request.device_id
        value = self.gateway.create(
            {"external_id": external_id, "name": request.name, "expires_at": request.expires_at},
            device_id=request.device_id,
            protocol=protocol,
        )
        row = self.gateway.store.get(client_id=value["client_id"])
        return self.describe(row, value)

    @bounded
    def lookup(self, device_id):
        rows = self.gateway.store.for_device(device_id)
        return {"connections": [self.get(row["client_id"], device_id).model_dump() for row in rows]}

    def owned(self, connection_id, device_id):
        row = self.gateway.store.get(client_id=connection_id)
        if row is None or row["device_id"] != device_id:
            raise OrchestratorError("not_found", 404)
        return row

    def describe(self, row, value):
        try:
            return Connection(
                connection_id=row["client_id"],
                device_id=row["device_id"],
                node_id=row["node_id"],
                protocol=row["protocol"],
                state=value["status"],
                expires_at=value["expires_at"],
            )
        except ValueError:
            raise OrchestratorError("node_response_invalid", 503) from None

    @bounded
    def get(self, connection_id, device_id):
        row = self.owned(connection_id, device_id)
        return self.describe(row, self.gateway.client(connection_id))

    @bounded
    def configuration(self, connection_id, request):
        with self.gateway.store.lock(connection_id):
            return self.configuration_locked(connection_id, request)

    def configuration_locked(self, connection_id, request):
        row = self.owned(connection_id, request.device_id)
        if not self.gateway.drivers.compatible(row["protocol"], request.capabilities):
            raise OrchestratorError("client_upgrade_required", 409)
        from orchestrator.application.leases import NodeLeases

        if NodeLeases(self.gateway).denied(row):
            raise OrchestratorError("access_unavailable", 410)
        snapshot = self.gateway.connection(connection_id)
        descriptor = self.describe(row, snapshot.client)
        if descriptor.state != "active" or datetime.fromisoformat(
            descriptor.expires_at.replace("Z", "+00:00")
        ) <= datetime.now(timezone.utc):
            raise OrchestratorError("access_unavailable", 410)
        # Reload binding after resolve() may have recovered a lost create response.
        row = self.gateway.store.get(client_id=connection_id)
        configuration = snapshot.configuration
        if configuration.protocol != row["protocol"] or not any(
            c.protocol == configuration.protocol
            and c.configuration_version == configuration.version
            for c in request.capabilities
        ):
            raise OrchestratorError("node_response_invalid", 503)
        revision = self.gateway.store.configuration_revision(
            row, configuration, descriptor.expires_at
        )
        return ConnectionConfiguration(
            **descriptor.model_dump(), revision=revision, configuration=configuration
        )

    @observed(Stage.EXPORT, identity=lambda self, connection_id, request: (connection_id, "export"))
    @bounded
    def export(self, connection_id, request):
        with self.gateway.store.lock(connection_id):
            row = self.owned(connection_id, request.device_id)
            self.gateway.require_settled(row)
            from orchestrator.application.leases import NodeLeases

            if NodeLeases(self.gateway).denied(row):
                raise OrchestratorError("access_unavailable", 410)
            node = self.gateway.node(row, issuance=True)
            driver = self.gateway.drivers.for_node(node)
            if request.format not in driver.capabilities.export_formats:
                raise OrchestratorError("unsupported_format", 422)
            descriptor = self.get(connection_id, request.device_id)
            if descriptor.state != "active" or datetime.fromisoformat(
                descriptor.expires_at.replace("Z", "+00:00")
            ) <= datetime.now(timezone.utc):
                raise OrchestratorError("access_unavailable", 410)
            row = self.owned(connection_id, request.device_id)
            document = driver.export(
                node, row["remote_id"], request.format, request.qr_content_format
            )
            # An export never creates, renews or switches an assignment. Recheck expiry
            # after the node request; disable/switch share the assignment lock.
            if datetime.fromisoformat(descriptor.expires_at.replace("Z", "+00:00")) <= datetime.now(
                timezone.utc
            ):
                raise OrchestratorError("access_unavailable", 410)
            return ConnectionExport(**descriptor.model_dump(), document=document)

    @bounded
    def mutate(self, connection_id, device_id, operation, payload):
        row = self.owned(connection_id, device_id)
        # Legacy node APIs expect the binding's external identity, not the slot ID.
        if operation == "replace":
            if payload["expected_external_id"] != device_id:
                raise OrchestratorError("not_found", 404)
            payload = {**payload, "expected_external_id": row["external_id"]}
        return self.describe(row, self.gateway.client(connection_id, operation, payload))

    @bounded
    def switch(self, connection_id, request):
        from orchestrator.application.switches import Switches

        with self.gateway.store.lock(connection_id):
            value = Switches(self.gateway).switch(connection_id, request)
            return self.describe(self.gateway.store.get(client_id=connection_id), value)
