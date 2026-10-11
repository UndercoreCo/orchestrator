"""Experimental adapter for the proposed Undercore TrustTunnel agent v1 API.

Explicit experimental opt-in; real TCP/UDP revoke, expiry and restart checks passed.
The stock TrustTunnel endpoint does not implement these routes.
"""

from datetime import datetime, timezone
import time
import tomllib
from typing import TypeVar, cast

from pydantic import TypeAdapter, ValidationError
from orchestrator.application.execution import bounded
from orchestrator.application.ports import DriverCapabilities, NodeConnection, NodeGrant
from orchestrator.application.telemetry import Stage, observed
from orchestrator.domain.contracts import (
    Contract,
    ExportDocument,
    Renewal,
    Replacement,
    TransportConfiguration,
)
from orchestrator.domain.models import Node, Observation, OrchestratorError
from orchestrator.domain.records import NodeAccess
from orchestrator.infrastructure.drivers.node_http import NodeHTTP
from orchestrator.infrastructure.drivers.trusttunnel_configuration import configuration
from orchestrator.infrastructure.drivers.trusttunnel_contract import (
    Client,
    ClientList,
    ClientSnapshot,
    ConnectionSnapshot,
    Create,
    Envelope,
    Health,
    RemoteID,
)

T = TypeVar("T", bound=Contract)
E = TypeVar("E", bound=Envelope)


class TrustTunnelAgentDriver:
    # Verified against the managed runtime bridge, not a stock endpoint or health claims alone.
    capabilities = DriverCapabilities(
        "trusttunnel", idempotent_create=True, enforces_expiry=True, confirmed_revoke=True
    )

    def __init__(self, api: NodeHTTP) -> None:
        self.api = api
        self.policy = api.policy

    @staticmethod
    def _parse(model: type[T], data: object) -> T:
        try:
            return model.model_validate(data)
        except ValidationError:
            raise OrchestratorError("node_response_invalid", 503) from None

    @staticmethod
    def _path(remote_id: str) -> str:
        try:
            TypeAdapter(RemoteID).validate_python(remote_id, strict=True)
        except ValidationError:
            raise OrchestratorError("node_identity_invalid", 503) from None
        return "/v1/clients/" + remote_id

    def _request(
        self,
        node: Node,
        model: type[E],
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> E:
        if node.protocol != "trusttunnel":
            raise OrchestratorError("node_identity_invalid", 503)
        if node.lease_enabled:
            raise OrchestratorError("control_lease_unavailable", 503)
        value = self._parse(model, self.api.request(node, method, path, payload, no_store=True))
        if value.server_id != node.server_id:
            raise OrchestratorError("node_identity_invalid", 503)
        return value

    def verify(self, node: Node) -> None:
        self._request(node, Health, "GET", "/v1/health")

    @observed(Stage.OBSERVE)
    def observe(self, node: Node) -> Observation:
        started = time.time()
        return Observation(node, len(self.list(node)), node.capacity, started)

    @bounded
    def list(self, node: Node) -> list[NodeAccess]:
        self.verify(node)
        value = self._request(node, ClientList, "GET", "/v1/clients")
        clients = [self.validate(client.model_dump()) for client in value.clients]
        if len({c["client_id"] for c in clients}) != len(clients) or len(
            {c["external_id"] for c in clients}
        ) != len(clients):
            raise OrchestratorError("node_response_invalid", 503)
        return clients

    @staticmethod
    def _input(model: type[T], payload: dict[str, object]) -> dict[str, object]:
        try:
            return model.model_validate(payload).model_dump()
        except ValidationError:
            raise OrchestratorError("invalid_request", 422) from None

    @bounded
    def create(self, node: Node, grant: NodeGrant) -> NodeAccess:
        payload = self._input(
            Create,
            {
                "external_id": grant.binding_key,
                "name": grant.name,
                "expires_at": grant.expires_at,
            },
        )
        self.verify(node)
        value = self._request(node, ClientSnapshot, "POST", "/v1/clients", payload)
        client = self.validate(value.client.model_dump(), external_id=grant.binding_key)
        if client["expires_at"] != grant.expires_at:
            raise OrchestratorError("node_response_invalid", 503)
        return client

    @bounded
    def get(self, node: Node, remote_id: str) -> NodeAccess:
        path = self._path(remote_id)
        self.verify(node)
        value = self._request(node, ClientSnapshot, "GET", path)
        return self.validate(value.client.model_dump(), client_id=remote_id)

    @bounded
    def mutate(
        self, node: Node, remote_id: str, operation: str, payload: dict[str, object]
    ) -> NodeAccess:
        path = self._path(remote_id)
        if operation == "renew":
            body = self._input(Renewal, payload)
        elif operation == "replace":
            body = self._input(Replacement, payload)
        elif operation in ("enable", "disable"):
            body = self._input(Contract, payload)
        else:
            raise OrchestratorError("unsupported_operation", 422)
        self.verify(node)
        value = self._request(node, ClientSnapshot, "POST", path + "/" + operation, body)
        expected = body.get("expected_external_id")
        client = self.validate(
            value.client.model_dump(),
            external_id=expected if isinstance(expected, str) else None,
            client_id=remote_id,
        )
        if (
            (operation == "disable" and client["status"] not in ("disabled", "expired"))
            or (operation == "enable" and client["status"] != "active")
            or ("expires_at" in body and client["expires_at"] != body["expires_at"])
        ):
            raise OrchestratorError("node_response_invalid", 503)
        if operation == "enable" or (operation == "replace" and body.get("allow_create")):
            self._require_active(client)
        return client

    @staticmethod
    def _require_active(client: NodeAccess) -> None:
        if client["status"] != "active" or datetime.fromisoformat(
            client["expires_at"].replace("Z", "+00:00")
        ) <= datetime.now(timezone.utc):
            raise OrchestratorError("access_unavailable", 410)

    def validate(
        self, data: object, external_id: str | None = None, client_id: str | None = None
    ) -> NodeAccess:
        client = self._parse(Client, data)
        if (external_id is not None and client.external_id != external_id) or (
            client_id is not None and client.client_id != client_id
        ):
            raise OrchestratorError("node_identity_invalid", 503)
        return cast(NodeAccess, client.model_dump())

    @bounded
    def _connection(self, node: Node, remote_id: str, binding_key: str | None) -> NodeConnection:
        path = self._path(remote_id)
        self.verify(node)
        try:
            value = self._request(node, ConnectionSnapshot, "GET", path + "/connection")
        except OrchestratorError as error:
            if error.status == 410:
                raise OrchestratorError("access_unavailable", 410) from None
            raise
        client = self.validate(value.client.model_dump(), binding_key, remote_id)
        self._require_active(client)
        try:
            document = configuration(
                value.endpoint_export,
                ipv6_available=value.ipv6_available,
                transport=value.transport,
            )
            if tomllib.loads(document.data)["endpoint"]["username"] != remote_id:
                raise ValueError()
        except ValueError:
            raise OrchestratorError("node_response_invalid", 503) from None
        return NodeConnection(client, document)

    def connection(self, node: Node, remote_id: str, binding_key: str) -> NodeConnection:
        return self._connection(node, remote_id, binding_key)

    def configuration(self, node: Node, remote_id: str) -> TransportConfiguration:
        return self._connection(node, remote_id, None).configuration

    def export(
        self, node: Node, remote_id: str, format: str, qr_content_format: str = "conf"
    ) -> ExportDocument:
        # File/deep-link/QR formats require their own public contract and client tests.
        raise OrchestratorError("unsupported_format", 422)
