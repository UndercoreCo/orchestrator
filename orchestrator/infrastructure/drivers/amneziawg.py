"""Amnezia implementation of the node lifecycle port."""

from orchestrator.domain.models import Node, Observation
from orchestrator.domain.records import NodeAccess, Mutation
from orchestrator.domain.contracts import ExportDocument
from orchestrator.application.ports import NodeGrant
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI
import re
from orchestrator.domain.contracts import TransportConfiguration
from orchestrator.domain.models import OrchestratorError
from orchestrator.application.ports import DriverCapabilities, NodeConnection
from orchestrator.infrastructure.drivers.exports import AmneziaExports
from orchestrator.infrastructure.drivers.amnezia_profile import guest_profile


class AmneziaAgentDriver:
    capabilities = DriverCapabilities(
        "amneziawg",
        idempotent_create=True,
        enforces_expiry=True,
        confirmed_revoke=True,
        export_formats=AmneziaExports.formats,
    )

    def __init__(self, api: AgentAPI) -> None:
        self.api = api

    def usage(self, node: Node, remote_id: str, binding_key: str) -> list[dict]:
        from orchestrator.domain.usage import UsageSample

        try:
            value = self.api.request(
                node, "GET", "/v1/clients/" + remote_id + "/usage", snapshot=True
            )
        except OrchestratorError as error:
            if error.status in (404, 405, 422):
                raise OrchestratorError("usage_unsupported", 422) from None
            raise
        self.api.validate_identity(node, value)
        if (
            value.get("schema_version") != 1
            or value.get("client_id") != remote_id
            or value.get("external_id") != binding_key
            or not isinstance(value.get("samples"), list)
            or len(value["samples"]) > 64
        ):
            raise OrchestratorError("counter_invalid", 503)
        try:
            return [UsageSample.model_validate(sample).model_dump() for sample in value["samples"]]
        except ValueError:
            raise OrchestratorError("counter_invalid", 503) from None

    def verify(self, node: Node) -> None:
        self.api.verify(node)

    def observe(self, node: Node) -> Observation:
        return self.api.observe(node)

    def list(self, node: Node) -> list[NodeAccess]:
        return self.api.list(node)

    def create(self, node: Node, grant: NodeGrant) -> NodeAccess:
        payload = {
            "external_id": grant.binding_key,
            "device_id": "account-v1",
            "name": grant.name,
            "expires_at": grant.expires_at,
        }
        return self.validate(self.api.request(node, "POST", "/v1/clients", payload))

    def get(self, node: Node, remote_id: str) -> NodeAccess:
        return self.validate(self.api.request(node, "GET", "/v1/clients/" + remote_id))

    def mutate(
        self, node: Node, remote_id: str, operation: str, payload: dict[str, object]
    ) -> NodeAccess:
        if operation not in {value.value for value in Mutation}:
            raise OrchestratorError("unsupported_operation", 422)
        return self.validate(
            self.api.request(node, "POST", "/v1/clients/" + remote_id + "/" + operation, payload)
        )

    def validate(
        self, data: object, external_id: str | None = None, client_id: str | None = None
    ) -> NodeAccess:
        return self.api.validate(data, external_id, client_id)

    def legacy_export(self, node: Node, remote_id: str, format: str) -> str:
        if format == "amnezia":
            return guest_profile(self.legacy_export(node, remote_id, "configuration"))
        if format not in ("configuration", "amnezia"):
            raise OrchestratorError("unsupported_format", 422)
        body = self.api.request(node, "GET", "/v1/clients/" + remote_id + "/" + format, text=True)
        if (format == "configuration" and ("[Interface]" not in body or "[Peer]" not in body)) or (
            format == "amnezia" and not re.fullmatch(r"vpn://[A-Za-z0-9_-]+", body)
        ):
            raise OrchestratorError("node_response_invalid", 503)
        return body

    def export(
        self, node: Node, remote_id: str, format: str, qr_content_format: str = "conf"
    ) -> ExportDocument:
        return AmneziaExports().export(
            lambda source: self.legacy_export(
                node, remote_id, {"conf": "configuration", "amnezia-vpn": "amnezia"}[source]
            ),
            format,
            qr_content_format,
        )

    def connection(self, node: Node, remote_id: str, binding_key: str) -> NodeConnection:
        try:
            snapshot = self.api.connection(node, remote_id)
        except OrchestratorError as error:
            if error.status == 410:
                raise OrchestratorError("access_unavailable", 410) from None
            raise
        if snapshot is None:
            client = self.get(node, remote_id)
            if (
                self.validate(client, external_id=binding_key, client_id=remote_id)["status"]
                != "active"
            ):
                raise OrchestratorError("access_unavailable", 410)
            return NodeConnection(client, self.configuration(node, remote_id))
        body = snapshot.get("configuration")
        if (
            not isinstance(body, str)
            or len(body.encode()) > 65536
            or "[Interface]" not in body
            or "[Peer]" not in body
        ):
            raise OrchestratorError("node_response_invalid", 503)
        return NodeConnection(
            self.validate(snapshot.get("client"), external_id=binding_key, client_id=remote_id),
            TransportConfiguration(protocol="amneziawg", format="awg-quick", data=body),
        )

    def configuration(self, node: Node, remote_id: str) -> TransportConfiguration:
        return TransportConfiguration(
            protocol="amneziawg",
            format="awg-quick",
            data=self.legacy_export(node, remote_id, "configuration"),
        )
