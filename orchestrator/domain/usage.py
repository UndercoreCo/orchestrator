"""Versioned raw measurements. No aggregation or quota decisions."""

from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UsageSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    counter_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(ge=1, le=2**63 - 1)
    measurement_basis: Literal["awg-transfer-v1"]
    observed_at: str
    started_at: str
    download_bytes: str
    upload_bytes: str
    baseline_download_bytes: str
    baseline_upload_bytes: str
    is_final: bool
    complete: bool

    @field_validator(
        "download_bytes", "upload_bytes", "baseline_download_bytes", "baseline_upload_bytes"
    )
    @classmethod
    def byte_count(cls, value):
        if (
            not value.isascii()
            or not value.isdecimal()
            or str(int(value)) != value
            or len(value) > 19
            or int(value) > 2**63 - 1
        ):
            raise ValueError("invalid byte counter")
        return value

    @field_validator("observed_at", "started_at")
    @classmethod
    def timestamp(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if (
            parsed.tzinfo != timezone.utc
            or parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != value
        ):
            raise ValueError("invalid timestamp")
        return value

    @model_validator(mode="after")
    def monotonic(self):
        if (
            self.started_at > self.observed_at
            or int(self.baseline_download_bytes) > int(self.download_bytes)
            or int(self.baseline_upload_bytes) > int(self.upload_bytes)
        ):
            raise ValueError("invalid baseline")
        return self


class UsageTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    connection_id: str = Field(pattern=r"^wgapi_[a-f0-9]{32}$")
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class UsageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    connections: list[UsageTarget] = Field(min_length=1, max_length=20)


class UsageError(UsageTarget):
    status: Literal["error"]
    error: Literal[
        "usage_unsupported",
        "counter_invalid",
        "usage_history_limit",
        "allocation_not_found",
        "not_found",
        "node_unavailable",
    ]
    retryable: bool


class AllocationUsage(UsageTarget):
    node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    allocation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    protocol: Literal["amneziawg", "trusttunnel"]
    status: Literal["ok"]
    samples: list[UsageSample] = Field(max_length=64)


class AllocationError(UsageError):
    node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    allocation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    protocol: Literal["amneziawg", "trusttunnel"]


class ConnectionUsage(AllocationUsage):
    previous: list[AllocationUsage | AllocationError] = Field(max_length=100)


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    results: list[ConnectionUsage | UsageError] = Field(max_length=20)
