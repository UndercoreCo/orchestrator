"""Typed journal and driver records. No transport documents belong in journal rows."""

from enum import StrEnum
from typing import Literal, NotRequired, TypedDict
from dataclasses import dataclass


class SwitchState(StrEnum):
    RESERVED = "reserved"
    SOURCE_REVOKED = "source_revoked"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class Mutation(StrEnum):
    RENEW = "renew"
    REPLACE = "replace"
    ENABLE = "enable"
    DISABLE = "disable"


class Assignment(TypedDict):
    external_id: str
    client_id: str
    node_id: str
    server_id: str
    creation_expires_at: str
    remote_id: str | None
    created_at: float
    last_operation: str | None
    last_outcome: str | None
    checked_at: float | None
    device_id: str
    protocol: Literal["amneziawg", "trusttunnel"]
    configuration_revision: int
    configuration_digest: str | None
    binding_key: str


class SwitchOperation(TypedDict):
    client_id: str
    operation_id: str
    source_node: str
    target_node: str
    target_server: str
    binding_key: str
    name: str
    expires_at: str
    state: str
    created_at: float
    disable_requested: int


class NodeAccess(TypedDict):
    client_id: str
    external_id: str
    device_id: NotRequired[str]
    name: str
    expires_at: str
    status: str
    created_at: str
    updated_at: str
    connection_checked_at: NotRequired[str | None]
    last_handshake_at: NotRequired[str | None]
    replacement_id: NotRequired[str | None]
    replacement_status: NotRequired[str | None]


class LeaseState(TypedDict):
    node_id: str
    server_id: str
    controller: str
    sequence: int
    valid_until: float
    verified: int
    fenced: int
    fence_boot: str
    fence_after: float
    fence_window_seconds: int


class CachedAccess(TypedDict):
    client_id: str
    binding_key: str
    payload: str
    denied: int


class RetiredBinding(TypedDict):
    node_id: str
    server_id: str
    remote_id: str
    binding_key: str
    cleaned: int


class InventoryRecord(TypedDict):
    id: str
    revision: int
    configuration: str


@dataclass(frozen=True)
class LeaseObservation:
    server_time: float
    required: bool
    controller_id: str | None
    sequence: int
    max_offline_seconds: int = 0


@dataclass(frozen=True)
class LeaseGrant:
    controller_id: str
    sequence: int
    valid_until: str
    offline_until: str | None = None
