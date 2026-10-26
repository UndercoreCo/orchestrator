"""Adapter to Undercore's durable Amnezia agent."""

from typing import cast
import re
import time
from orchestrator.domain.records import NodeAccess
from orchestrator.application.execution import bounded
from orchestrator.application.telemetry import Stage, observed
from orchestrator.domain.models import Observation, OrchestratorError
from orchestrator.infrastructure.drivers.node_http import NodeHTTP


class AgentAPI(NodeHTTP):
    @bounded
    def request(self, node, method, path, payload=None, text=False, *, snapshot=False):
        if path != "/v1/health" and not snapshot:
            self.verify(node)
        return super().request(
            node,
            method,
            path,
            payload,
            text,
            no_store=snapshot,
            legacy_post_route=snapshot,
        )

    def verify(self, node):
        health = self.request(node, "GET", "/v1/health")
        self.validate_identity(node, health)

    @staticmethod
    def validate_identity(node, health):
        if (
            not isinstance(health, dict)
            or health.get("server_id") != node.server_id
            or health.get("status") != "ok"
            or health.get("protocol") != "amneziawg"
        ):
            raise OrchestratorError("node_identity_invalid", 503)

    def connection(self, node, remote_id):
        # The combined response authenticates the node itself. Only an absent route
        # falls back to the legacy protocol (including its health verification).
        try:
            value = self.request(
                node, "GET", "/v1/clients/" + remote_id + "/connection", snapshot=True
            )
        except OrchestratorError as error:
            if error.code == "node_request_failed" and error.status in (404, 405):
                return None
            raise
        self.validate_identity(node, value)
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise OrchestratorError("node_response_invalid", 503)
        return value

    @observed(Stage.OBSERVE)
    def observe(self, node):
        started = time.time()
        # list() already verifies this node immediately before its read.
        clients = self.list(node)
        return Observation(node, len(clients), node.capacity, started)

    def list(self, node):
        data = self.request(node, "GET", "/v1/clients")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("clients"), list)
            or len(data["clients"]) > 10000
        ):
            raise OrchestratorError("node_response_invalid", 503)
        return [self.validate(value) for value in data["clients"]]

    @staticmethod
    def validate(
        data: object, external_id: str | None = None, client_id: str | None = None
    ) -> NodeAccess:
        fields = (
            "client_id",
            "external_id",
            "device_id",
            "name",
            "expires_at",
            "status",
            "created_at",
            "updated_at",
        )
        if (
            not isinstance(data, dict)
            or any(not isinstance(data.get(key), str) for key in fields)
            or not re.fullmatch(r"wgapi_[a-f0-9]{32}", data["client_id"])
            or data["device_id"] != "account-v1"
            or (external_id is not None and data["external_id"] != external_id)
            or (client_id is not None and data["client_id"] != client_id)
        ):
            raise OrchestratorError("node_identity_invalid", 503)
        allowed = (
            *fields,
            "connection_checked_at",
            "last_handshake_at",
            "replacement_id",
            "replacement_status",
        )
        return cast(NodeAccess, {key: data[key] for key in allowed if key in data})
