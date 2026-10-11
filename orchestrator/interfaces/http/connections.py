"""Internal-only v2 routes share the existing Bearer/no-store boundary."""

import json
from fastapi import Request
from pydantic import ValidationError
from orchestrator.application.connections import Connections
from orchestrator.domain.contracts import (
    DeviceOperation,
    RenewRequest,
    ReplaceRequest,
    ConfigurationRequest,
    ExportRequest,
    DeviceRequest,
    SwitchRequest,
    RecoveryRequest,
)
from orchestrator.domain.models import OrchestratorError


async def json_body(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8192:
            raise OrchestratorError("request_too_large", 413)
    try:
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise OrchestratorError("invalid_request", 422) from None


async def decode(request, model):
    try:
        return model.model_validate(await json_body(request))
    except ValidationError:
        # Never return submitted credentials or Pydantic input excerpts.
        raise OrchestratorError("invalid_request", 422) from None


def mount(app, gateway):
    from starlette.concurrency import run_in_threadpool

    service = Connections(gateway)

    @app.get("/internal/v2/capabilities")
    async def capabilities():
        return service.capabilities()

    @app.post("/internal/v2/connections")
    async def create(request: Request):
        value = await decode(request, DeviceRequest)
        result = await run_in_threadpool(service.create, value)
        return result.model_dump()

    @app.post("/internal/v2/connections/lookup")
    async def lookup(request: Request):
        value = await decode(request, DeviceOperation)
        return await run_in_threadpool(service.lookup, value.device_id)

    @app.get("/internal/v2/connections/{connection_id}")
    async def get(connection_id: str, device_id: str):
        result = await run_in_threadpool(service.get, connection_id, device_id)
        return result.model_dump()

    @app.post("/internal/v2/connections/{connection_id}/configuration")
    async def configuration(connection_id: str, request: Request):
        value = await decode(request, ConfigurationRequest)
        result = await run_in_threadpool(service.configuration, connection_id, value)
        return result.model_dump()

    @app.post("/internal/v2/connections/{connection_id}/export")
    async def export(connection_id: str, request: Request):
        value = await decode(request, ExportRequest)
        result = await run_in_threadpool(service.export, connection_id, value)
        return result.model_dump()

    @app.post("/internal/v2/connections/{connection_id}/recover")
    async def recover(connection_id: str, request: Request):
        from orchestrator.application.recovery import Recovery

        value = await decode(request, RecoveryRequest)
        result = await run_in_threadpool(Recovery(gateway).recover, connection_id, value)
        return result.model_dump()

    @app.post("/internal/v2/connections/{connection_id}/switch")
    async def switch(connection_id: str, request: Request):
        value = await decode(request, SwitchRequest)
        result = await run_in_threadpool(service.switch, connection_id, value)
        return result.model_dump()

    @app.post("/internal/v2/connections/{connection_id}/{operation}")
    async def mutate(connection_id: str, operation: str, request: Request):
        if operation not in ("renew", "replace", "enable", "disable"):
            raise OrchestratorError("not_found", 404)
        model = {
            "renew": RenewRequest,
            "replace": ReplaceRequest,
            "enable": DeviceOperation,
            "disable": DeviceOperation,
        }[operation]
        value = await decode(request, model)
        payload = value.model_dump(exclude={"device_id"}, exclude_unset=True)
        result = await run_in_threadpool(
            service.mutate, connection_id, value.device_id, operation, payload
        )
        return result.model_dump()
