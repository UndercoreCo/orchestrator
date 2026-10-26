"""Draft v1 contract for OUR TrustTunnel node agent, not the upstream endpoint API."""

from typing import Annotated, Literal
from pydantic import Field, field_validator
from orchestrator.domain.contracts import Contract, Identifier, Timestamp, VersionOne

RemoteID = Annotated[str, Field(pattern=r"^ttapi_[a-f0-9]{32}$")]
Name = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")]


class Client(Contract):
    client_id: RemoteID
    external_id: Identifier
    name: Name
    expires_at: Timestamp
    status: Literal["active", "disabled", "expired"]
    created_at: Timestamp
    updated_at: Timestamp


class Create(Contract):
    external_id: Identifier
    name: Name
    expires_at: Timestamp


class Envelope(Contract):
    schema_version: VersionOne
    server_id: Identifier
    protocol: Literal["trusttunnel"]
    status: Literal["ok"]


class Guarantees(Contract):
    idempotent_create: bool
    enforces_expiry: bool
    confirmed_revoke: bool

    @field_validator("idempotent_create", "enforces_expiry", "confirmed_revoke")
    @classmethod
    def required(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Required lifecycle guarantee missing")
        return value


class Health(Envelope):
    capabilities: Guarantees


class ClientSnapshot(Envelope):
    # True means the state is applied to the runtime, not only persisted in a journal.
    applied: bool
    client: Client

    @field_validator("applied")
    @classmethod
    def confirmed(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Runtime state is not confirmed")
        return value


class ClientList(Envelope):
    # Complete bounded inventory, never a silently truncated page.
    clients: list[Client] = Field(max_length=10000)


class ConnectionSnapshot(ClientSnapshot):
    endpoint_export: str = Field(min_length=1, max_length=65536, repr=False)
    ipv6_available: bool
    transport: Literal["http2", "http3"]


def schemas() -> dict[str, object]:
    from orchestrator.domain.contracts import Renewal, Replacement

    models = (Health, Create, Renewal, Replacement, ClientSnapshot, ClientList, ConnectionSnapshot)
    return {model.__name__: model.model_json_schema() for model in models}
