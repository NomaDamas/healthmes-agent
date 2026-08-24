"""Stable contracts consumed by future desktop and mobile settings UIs."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class InputConnectionState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    CONNECTED = "connected"
    UNAVAILABLE = "unavailable"


class InputCollectionState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    IDLE = "idle"
    COLLECTING = "collecting"
    PAUSED = "paused"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class InputActionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "authorize",
        "connect",
        "disconnect",
        "pause",
        "resume",
        "sync",
        "capture",
    ]
    execution: Literal["server", "device", "browser", "external", "local_cli"]
    method: Literal["GET", "POST", "PUT"] | None = None
    endpoint: str | None = None
    requires_instance: bool = False
    description: str


class InputSettingDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Literal[
        "enabled",
        "excluded_apps",
        "paused_until",
        "decision_access_enabled",
        "retention",
    ]
    value_type: Literal[
        "boolean",
        "datetime",
        "string_list",
        "retention_map",
    ]
    scope: Literal["instance", "domain", "data_class"]
    allowed_values: list[str] = Field(default_factory=list)
    description: str


class InputRetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_class: str
    preset: Literal["1d", "7d", "14d", "30d", "90d", "forever"]
    retention_days: int | None
    enabled: bool
    effective_preset: Literal[
        "1d",
        "7d",
        "14d",
        "30d",
        "90d",
        "forever",
        "disabled",
    ]
    shared_across_source_instances: bool = True


class InputPrivacyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_first: bool
    raw_content_collected: bool
    source_side_exclusions: bool
    default_llm_exposure: Literal[
        "none",
        "aggregate_only",
        "structured_only",
    ]
    notes: list[str] = Field(default_factory=list)


class InputInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str
    platform: str
    enabled: bool
    effective_collecting: bool
    permission_status: str
    capability: str
    blocked_reason: str | None = None
    status_reason: str | None = None
    status_observed_at: datetime | None = None
    last_collected_at: datetime | None = None
    last_uploaded_at: datetime | None = None
    coverage: float | None = Field(default=None, ge=0, le=1)
    excluded_apps: list[str] = Field(default_factory=list)
    paused_until: datetime | None = None
    config_revision: int = Field(ge=0)


class InputSourceDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    domain: Literal["activity", "nutrition", "wearable", "calendar"]
    display_name: str
    platforms: list[str]
    capabilities: list[str]
    connection_state: InputConnectionState
    collection_state: InputCollectionState
    decision_access_enabled: bool
    instances: list[InputInstance]
    retention: list[InputRetentionPolicy]
    settings: list[InputSettingDefinition]
    actions: list[InputActionDescriptor]
    privacy: InputPrivacyProfile
    limitations: list[str]
    revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class InputSourcesOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[InputSourceDescriptor]


class InputSettingsUpdate(BaseModel):
    """One source update without leaking source-specific implementation details."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str | None = Field(default=None, min_length=1, max_length=255)
    platform: str | None = Field(default=None, min_length=1, max_length=32)
    enabled: bool | None = None
    excluded_apps: list[str] | None = Field(default=None, max_length=500)
    paused_until: AwareDatetime | None = None
    decision_access_enabled: bool | None = None
    retention: dict[
        str,
        Literal["1d", "7d", "14d", "30d", "90d", "forever"],
    ] = Field(default_factory=dict, max_length=16)

    @field_validator("excluded_apps")
    @classmethod
    def normalize_excluded_apps(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None
        cleaned = sorted(
            {item.strip() for item in value if item.strip()},
            key=lambda item: (item.casefold(), item),
        )
        if any(len(item) > 255 for item in cleaned):
            raise ValueError(
                "excluded app identifiers must be at most 255 characters"
            )
        return cleaned

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, value: str | None) -> str | None:
        return value.strip().casefold() if value is not None else None

    @field_validator("paused_until", mode="after")
    @classmethod
    def normalize_paused_until(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> InputSettingsUpdate:
        has_change = (
            self.platform is not None
            or self.enabled is not None
            or self.excluded_apps is not None
            or "paused_until" in self.model_fields_set
            or self.decision_access_enabled is not None
            or bool(self.retention)
        )
        if not has_change:
            raise ValueError("at least one input setting must be changed")
        return self

    def collection_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if self.enabled is not None:
            values["enabled"] = self.enabled
        if self.excluded_apps is not None:
            values["excluded_apps"] = self.excluded_apps
        if "paused_until" in self.model_fields_set:
            values["paused_until"] = self.paused_until
        return values
