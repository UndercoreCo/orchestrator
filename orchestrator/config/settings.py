"""Pure configuration schema, with no I/O or driver construction."""

from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from orchestrator.config.policy import RuntimePolicy
from orchestrator.domain.inventory import NodeSettings


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    experimental_trusttunnel: bool = False
    schema_version: int = Field(default=1, ge=1, le=1)
    state_directory: str
    admin_token_file: str | None = None
    backend_token_file: str
    nodes: list[NodeSettings] = Field(default_factory=list, max_length=1000)
    policy: RuntimePolicy = Field(default_factory=RuntimePolicy)

    @field_validator("admin_token_file")
    @classmethod
    def optional_absolute(cls, value):
        if value is not None and not Path(value).is_absolute():
            raise ValueError("Absolute path required")
        return value

    @field_validator("state_directory", "backend_token_file")
    @classmethod
    def absolute_path(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("Absolute path required")
        return value
