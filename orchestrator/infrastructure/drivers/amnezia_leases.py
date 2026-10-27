"""Agent lease protocol translated to the protocol-neutral control lease port."""

from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI
from datetime import datetime
from orchestrator.domain.models import Node, OrchestratorError
from orchestrator.domain.records import LeaseObservation, LeaseGrant


class AmneziaLeaseTransport:
    def __init__(self, api: AgentAPI) -> None:
        self.api = api

    def probe(self, node: Node) -> None:
        health = self.api.request(node, "GET", "/v1/health")
        self.api.validate_identity(node, health)
        lease = health.get("control_lease")
        if (
            not isinstance(lease, dict)
            or type(lease.get("version")) is not int
            or lease["version"] != 1
        ):
            raise OrchestratorError("control_lease_unavailable", 409)

    def observe(self, node: Node) -> LeaseObservation:
        health = self.api.request(node, "GET", "/v1/health")
        self.api.validate_identity(node, health)
        try:
            lease = health["control_lease"]
            if type(lease["version"]) is not int or lease["version"] != 1:
                raise ValueError()
            required = lease.get("required", False)
            controller = lease.get("controller_id")
            sequence = lease.get("sequence", 0)
            if (
                type(required) is not bool
                or type(sequence) is not int
                or (controller is not None and not isinstance(controller, str))
            ):
                raise ValueError()
            offline_max = 0
            versions = lease.get("supported_versions", [1])
            if not isinstance(versions, list) or any(type(v) is not int for v in versions):
                raise ValueError()
            if 2 in versions:
                offline_max = lease["max_offline_seconds"]
                if type(offline_max) is not int or not 300 <= offline_max <= 86400:
                    raise ValueError()
            return LeaseObservation(
                datetime.fromisoformat(lease["server_time"].replace("Z", "+00:00")).timestamp(),
                required,
                controller,
                sequence,
                offline_max,
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise OrchestratorError("control_lease_unavailable", 503) from None

    def renew(self, node: Node, grant: LeaseGrant) -> None:
        payload: dict[str, object] = {
            "controller_id": grant.controller_id,
            "sequence": grant.sequence,
            "valid_until": grant.valid_until,
        }
        if grant.offline_until is not None:
            payload.update(version=2, offline_until=grant.offline_until)
        reply = self.api.request(
            node,
            "POST",
            "/v1/control-lease",
            payload,
        )
        if (
            not isinstance(reply, dict)
            or reply.get("required") is not True
            or reply.get("active") is not True
            or reply.get("controller_id") != grant.controller_id
            or type(reply.get("sequence")) is not int
            or reply["sequence"] != grant.sequence
            or reply.get("valid_until") != grant.valid_until
            or (
                grant.offline_until is not None
                and (
                    reply.get("offline_until") != grant.offline_until
                    or reply.get("fresh") is not True
                )
            )
        ):
            raise OrchestratorError("control_lease_unconfirmed", 503)
