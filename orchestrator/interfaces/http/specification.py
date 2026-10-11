"""Offline OpenAPI for new integrations; no settings, database or network required.

Runtime routes deliberately retain manual parsing to redact validation errors.
This module reuses their Pydantic contracts without enabling production Swagger.
"""

from orchestrator.domain import contracts
from orchestrator.version import current_version

PREFIX = "/internal/v2/connections"
IDENTIFIER = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,100}$"}


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


def object_schema(properties, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def document():
    schemas = {}
    for name in contracts.schemas():
        schema = getattr(contracts, name).model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        schemas.update(schema.pop("$defs", {}))
        schemas[name] = schema
    schemas["ConnectionList"] = object_schema(
        {"connections": {"type": "array", "items": ref("Connection")}}
    )
    schemas["Error"] = object_schema({"detail": {"type": "string"}})
    schemas["Health"] = object_schema(
        {
            "status": {"const": "ok"},
            "protocol": {"const": "amneziawg"},
            "mode": {"const": "pilot", "description": "Legacy compatibility field."},
            "formats": {"type": "array", "items": {"type": "string"}},
            "service": {"const": "undercore-orchestrator"},
            "version": {"type": "string"},
            "contract_version": {"const": 2},
        }
    )
    schemas["Capabilities"] = object_schema(
        {
            "schema_version": {"const": 1},
            "protocols": {
                "type": "array",
                "items": object_schema(
                    {
                        "protocol": {"type": "string"},
                        "configuration_version": {"type": "integer"},
                        "idempotent_create": {"type": "boolean"},
                        "enforces_expiry": {"type": "boolean"},
                        "confirmed_revoke": {"type": "boolean"},
                        "export_formats": {"type": "array", "items": {"type": "string"}},
                    }
                ),
            },
        }
    )
    schemas["Nodes"] = object_schema(
        {
            "nodes": {
                "type": "array",
                "items": object_schema(
                    {
                        "id": {"type": "string"},
                        "region": {"type": "string"},
                        "mode": {"enum": ["active", "draining", "disabled"]},
                        "assigned": {"type": "integer"},
                        "capacity": {"type": "integer"},
                        "pending": {"type": "integer"},
                        "last_operation": {"type": ["string", "null"]},
                        "last_outcome": {"type": ["string", "null"]},
                        "checked_at": {"type": ["number", "null"]},
                    }
                ),
            }
        }
    )
    paths = {}

    def operation(path, method, name, response, body=None, description=""):
        value = {
            "operationId": name,
            "summary": name.replace("_", " ").capitalize(),
            "description": description,
            "responses": {
                "200": {
                    "description": "Successful operation. Tunnel connectivity is not implied.",
                    "headers": {
                        "Cache-Control": {
                            "schema": {"type": "string"},
                            "description": "private, no-store",
                        }
                    },
                    "content": {"application/json": {"schema": ref(response)}},
                },
                "default": {
                    "description": (
                        "Application error: detail is a machine code (contracts/errors.json). "
                        "401 authorization; 404 ownership/not found; 409 conflict; 410 unavailable; "
                        "413/422 invalid input; 429 cooldown; 503 unavailable or pending lease. "
                        "Framework query-validation errors can instead contain a detail array. "
                        "Proxies may return non-JSON errors. Repeat only the same operation identity."
                    ),
                    "content": {
                        "application/json": {
                            "schema": {
                                "oneOf": [
                                    ref("Error"),
                                    object_schema(
                                        {"detail": {"type": "array", "items": {"type": "object"}}}
                                    ),
                                ]
                            }
                        }
                    },
                },
            },
        }
        if "{connection_id}" in path:
            value["parameters"] = [
                {"name": "connection_id", "in": "path", "required": True, "schema": IDENTIFIER}
            ]
        if method == "get" and path == PREFIX + "/{connection_id}":
            value["parameters"].append(
                {"name": "device_id", "in": "query", "required": True, "schema": IDENTIFIER}
            )
        if body:
            value["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": ref(body)}},
            }
        paths.setdefault(path, {})[method] = value

    operation(
        "/v1/health", "get", "health", "Health", description="Process health only; authenticated."
    )
    operation(
        "/internal/v1/nodes",
        "get",
        "list_nodes",
        "Nodes",
        description="Registry and assignment metadata, not live speed tests.",
    )
    operation("/internal/v2/capabilities", "get", "capabilities", "Capabilities")
    operation(
        PREFIX,
        "post",
        "create_connection",
        "Connection",
        "DeviceRequest",
        "Repeat the identical request with the same device_id after a lost response.",
    )
    operation(PREFIX + "/lookup", "post", "lookup_connections", "ConnectionList", "DeviceOperation")
    operation(PREFIX + "/{connection_id}", "get", "get_connection", "Connection")
    for action, request, response, description in (
        (
            "configuration",
            "ConfigurationRequest",
            "ConnectionConfiguration",
            "Secret transport document. Never log response bodies.",
        ),
        (
            "export",
            "ExportRequest",
            "ConnectionExport",
            "Secret export of the existing assignment; never creates a new slot. QR requires format=qr.",
        ),
        (
            "recover",
            "RecoveryRequest",
            "ConnectionConfiguration",
            "Repeat the same expected_revision; respect cooldown and lease waiting.",
        ),
        (
            "switch",
            "SwitchRequest",
            "Connection",
            "Operator action. Same-protocol switch; repeat the same operation key and expected node.",
        ),
        (
            "renew",
            "RenewRequest",
            "Connection",
            "Repeat the same idempotency_key and expiry after a timeout.",
        ),
        (
            "replace",
            "ReplaceRequest",
            "Connection",
            "Compatibility replacement; expected_external_id must equal device_id.",
        ),
        (
            "disable",
            "DeviceOperation",
            "Connection",
            "A timeout does not confirm revocation. Repeat and verify state.",
        ),
        (
            "enable",
            "DeviceOperation",
            "Connection",
            "Does not extend expiry or grant a new entitlement.",
        ),
    ):
        operation(
            PREFIX + "/{connection_id}/" + action,
            "post",
            action + "_connection",
            response,
            request,
            description,
        )
    paths["/internal/v1/metrics"] = {
        "get": {
            "operationId": "metrics",
            "summary": "Local operational metrics; no VPN node probes",
            "responses": {
                "200": {
                    "description": "Prometheus text exposition",
                    "content": {"text/plain": {"schema": {"type": "string"}}},
                },
                "401": {"description": "Missing or invalid backend token"},
            },
        }
    }
    for operations in paths.values():
        for operation in operations.values():
            for response in operation["responses"].values():
                response.setdefault("headers", {})["X-Request-ID"] = {
                    "description": "Server-generated correlation ID; caller input is not trusted",
                    "schema": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Undercore Orchestrator — backend API",
            "version": current_version(),
            "license": {"name": "MIT", "identifier": "MIT"},
            "description": (
                "New integrations: v2 plus process health and node overview. Legacy v1 client "
                "operations are documented separately in docs/public/api.md. Trusted backend "
                "only; it authorizes users and slots. Bodies are limited to 8192 bytes. "
                "Runtime validation includes identity, expiry and state checks beyond JSON Schema. "
                "HTTPS is required off loopback. This artifact never enables live Swagger."
            ),
        },
        "servers": [{"url": "http://127.0.0.1:8792", "description": "Local development only"}],
        "security": [{"backendToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"backendToken": {"type": "http", "scheme": "bearer"}},
            "schemas": schemas,
        },
    }
