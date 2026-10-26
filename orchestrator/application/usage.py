"""Read-through raw telemetry. Deliberately has no usage database or arithmetic."""

import hashlib
from orchestrator.application.connections import Connections
from orchestrator.application.execution import bounded
from orchestrator.domain.models import OrchestratorError


class Usage:
    def __init__(self, gateway):
        self.gateway = gateway

    @bounded
    def batch(self, request):
        results = []
        for target in request.connections:
            identity = target.model_dump()
            try:
                # Ownership is checked before any node I/O, including for revoked access.
                row = Connections(self.gateway).owned(target.connection_id, target.device_id)
                node = self.gateway.node(row)
                driver = self.gateway.drivers.for_node(node)
                reader = getattr(driver, "usage", None)
                if reader is None:
                    raise OrchestratorError("usage_unsupported", 422)
                if not row["remote_id"]:
                    raise OrchestratorError("allocation_not_found", 404)
                samples = reader(node, row["remote_id"], row["binding_key"])
                # Mapping identity only; never store counters in the assignment journal.
                allocation = hashlib.sha256(
                    (node.server_id + ":" + row["remote_id"]).encode()
                ).hexdigest()
                previous = []
                bindings = self.gateway.store.usage_bindings(row)
                if len(bindings) > 100:
                    raise OrchestratorError("usage_history_limit", 503)
                for binding in bindings:
                    if (
                        binding["remote_id"] == row["remote_id"]
                        and binding["server_id"] == row["server_id"]
                    ):
                        continue
                    old_allocation = hashlib.sha256(
                        (binding["server_id"] + ":" + binding["remote_id"]).encode()
                    ).hexdigest()
                    old_identity = {
                        **identity,
                        "node_id": binding["node_id"],
                        "allocation_id": old_allocation,
                        "protocol": row["protocol"],
                    }
                    try:
                        old_node = self.gateway.node({**row, **binding})
                        old_driver = self.gateway.drivers.for_node(old_node)
                        old_samples = old_driver.usage(
                            old_node, binding["remote_id"], binding["binding_key"]
                        )
                        previous.append({**old_identity, "status": "ok", "samples": old_samples})
                    except OrchestratorError:
                        previous.append(
                            {
                                **old_identity,
                                "status": "error",
                                "error": "node_unavailable",
                                "retryable": True,
                            }
                        )
                results.append(
                    {
                        **identity,
                        "status": "ok",
                        "node_id": node.id,
                        "allocation_id": allocation,
                        "protocol": row["protocol"],
                        "samples": samples,
                        "previous": previous,
                    }
                )
            except OrchestratorError as error:
                code = (
                    error.code
                    if error.code
                    in {
                        "usage_unsupported",
                        "counter_invalid",
                        "usage_history_limit",
                        "allocation_not_found",
                        "not_found",
                    }
                    else "node_unavailable"
                )
                results.append(
                    {
                        **identity,
                        "status": "error",
                        "error": code,
                        "retryable": code in {"node_unavailable", "counter_invalid"},
                    }
                )
        from orchestrator.domain.usage import UsageResponse

        return UsageResponse.model_validate({"schema_version": 1, "results": results}).model_dump()
