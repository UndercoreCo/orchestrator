"""Validated runtime limits. Missing optional fields use conservative defaults."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class AgentPolicy(PolicyModel):
    operation_timeout_seconds: int = Field(default=30, ge=5, le=60)
    queue_timeout_seconds: int = Field(default=2, ge=1, le=5)
    max_concurrent_requests: int = Field(default=16, ge=2, le=64)
    max_concurrent_per_node: int = Field(default=4, ge=1, le=32)
    max_pending_per_node: int = Field(default=8, ge=1, le=64)
    max_pending_requests: int = Field(default=64, ge=2, le=256)
    connect_timeout_seconds: int = Field(default=4, ge=1, le=10)
    response_timeout_seconds: int = Field(default=12, ge=1, le=15)

    @model_validator(mode="after")
    def concurrency_bounds(self):
        if not self.max_concurrent_per_node < self.max_concurrent_requests:
            raise ValueError("Per-node limit must leave room for another node")
        if self.max_pending_requests < self.max_concurrent_requests:
            raise ValueError("Pending limit must cover active requests")
        if (
            not self.max_concurrent_per_node
            <= self.max_pending_per_node
            < self.max_pending_requests
        ):
            raise ValueError(
                "Per-node pending limit must cover its active limit and leave room for another node"
            )
        return self


class RecoveryPolicy(PolicyModel):
    cooldown_seconds: int = Field(default=1800, ge=60, le=86400)
    reconcile_batch_size: int = Field(default=16, ge=1, le=100)


class SelectionPolicy(PolicyModel):
    observation_workers: int = Field(default=4, ge=1, le=16)
    observation_max_age_seconds: int = Field(default=30, ge=1, le=300)


class LeasePolicy(PolicyModel):
    offline_duration_seconds: int = Field(default=0, ge=0, le=3600)
    duration_seconds: int = Field(default=90, ge=90, le=120)
    max_node_lease_seconds: int = Field(default=120, ge=120, le=300)
    fence_grace_seconds: int = Field(default=125, ge=125, le=600)
    heartbeat_workers: int = Field(default=64, ge=64, le=64)

    @model_validator(mode="after")
    def safe_bounds(self):
        if 0 < self.offline_duration_seconds < 300:
            raise ValueError("Offline permission must be disabled or at least 300 seconds")
        if self.duration_seconds > self.max_node_lease_seconds:
            raise ValueError("Lease duration exceeds the node lease bound")
        return self


class RuntimePolicy(PolicyModel):
    agents: AgentPolicy = Field(default_factory=AgentPolicy)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)
    leases: LeasePolicy = Field(default_factory=LeasePolicy)
