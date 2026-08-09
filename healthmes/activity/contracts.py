"""Device-neutral contracts for phone and computer activity telemetry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

RESERVED_ACTIVITY_PROVIDER_NAMES = frozenset(
    {"activitywatch", "android-usage", "ios-device-activity"}
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def is_reserved_activity_provider(value: str) -> bool:
    normalized = value.casefold()
    return (
        normalized in RESERVED_ACTIVITY_PROVIDER_NAMES
        or normalized.startswith("healthmes-activity-")
    )


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone must be a valid IANA name, got {value!r}") from exc
    return value


class ActivityPlatform(StrEnum):
    ANDROID = "android"
    IOS = "ios"
    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"
    UNKNOWN = "unknown"


class ActivityCapability(StrEnum):
    DETAILED = "detailed"
    AGGREGATE = "aggregate"
    UNAVAILABLE = "unavailable"


class ActivityPermissionStatus(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ActivityState(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    LOCKED = "locked"


class AppHourRecord(BaseModel):
    """One source-reported app aggregate inside an hourly UTC bucket."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["app_hour"] = "app_hour"
    source_record_id: str = Field(min_length=1, max_length=255)
    bucket_start: AwareDatetime
    app_id: str = Field(min_length=1, max_length=255)
    # The legacy Android endpoint historically accepted up to 24 hours.
    # Aggregation clamps impossible over-reporting to the wall-clock bucket
    # and emits a limitation instead of breaking that compatibility surface.
    foreground_seconds: int = Field(ge=0, le=24 * 3600)
    launches: int = Field(default=0, ge=0, le=100_000)
    category: str | None = Field(default=None, max_length=64)
    coverage_seconds: int | None = Field(default=None, ge=0, le=3600)
    bucket_complete: bool = True

    @field_validator("bucket_start", mode="after")
    @classmethod
    def normalize_bucket_start(cls, value: datetime) -> datetime:
        return _utc(value)


class AppIntervalRecord(BaseModel):
    """One closed foreground, idle, or locked interval."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["app_interval"] = "app_interval"
    source_record_id: str = Field(min_length=1, max_length=255)
    source_group_id: str | None = Field(default=None, max_length=255)
    start_at: AwareDatetime
    end_at: AwareDatetime
    state: ActivityState
    app_id: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=64)
    launches: int = Field(default=0, ge=0, le=100_000)

    @field_validator("start_at", "end_at", mode="after")
    @classmethod
    def normalize_interval_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_interval(self) -> AppIntervalRecord:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if (self.end_at - self.start_at).total_seconds() > 24 * 3600:
            raise ValueError("one activity interval cannot exceed 24 hours")
        if self.state is ActivityState.ACTIVE and not self.app_id:
            raise ValueError("active intervals require app_id")
        if self.state is not ActivityState.ACTIVE and self.app_id is not None:
            raise ValueError("idle/locked intervals must not carry app identity")
        return self


ActivityRecord = Annotated[
    AppHourRecord | AppIntervalRecord,
    Field(discriminator="kind"),
]


class ActivityBatchIn(BaseModel):
    """Canonical device batch accepted by HealthMes Activity Ingest."""

    model_config = ConfigDict(extra="forbid")

    source_provider: str = Field(min_length=1, max_length=64)
    source_device: str = Field(min_length=1, max_length=255)
    platform: ActivityPlatform
    capability: ActivityCapability
    timezone: str = Field(min_length=1, max_length=64)
    collected_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    collection_revision: int | None = Field(default=None, ge=0)
    records: list[ActivityRecord] = Field(min_length=1, max_length=5000)

    @field_validator("timezone")
    @classmethod
    def validate_batch_timezone(cls, value: str) -> str:
        return validate_timezone(value)

    @field_validator("source_provider")
    @classmethod
    def protect_internal_provider_namespace(cls, value: str) -> str:
        if value.casefold().startswith("healthmes-activity-"):
            raise ValueError("healthmes-activity-* providers are reserved for the engine")
        return value

    @field_validator("collected_at", mode="after")
    @classmethod
    def normalize_collected_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_capability(self) -> ActivityBatchIn:
        if self.capability is ActivityCapability.UNAVAILABLE:
            raise ValueError("unavailable collectors cannot submit activity records")
        if self.platform is ActivityPlatform.IOS and self.capability is ActivityCapability.DETAILED:
            raise ValueError("iOS detailed app timelines are not an MVP capability")
        if self.capability is ActivityCapability.AGGREGATE and any(
            not isinstance(record, AppHourRecord) for record in self.records
        ):
            raise ValueError("aggregate collectors may submit only app_hour records")
        if self.capability is ActivityCapability.DETAILED and any(
            not isinstance(record, AppIntervalRecord) for record in self.records
        ):
            raise ValueError("detailed collectors may submit only app_interval records")
        return self


class ActivityBatchOut(BaseModel):
    accepted: int
    created: int
    updated: int
    duplicates: int
    excluded: int
    tombstoned: int = 0
    affected_dates: list[str]


class ActivityCollectionUpdate(BaseModel):
    """Configuration returned to collectors and rendered by future device UI."""

    model_config = ConfigDict(extra="forbid")

    platform: ActivityPlatform | None = None
    enabled: bool | None = None
    excluded_apps: list[str] | None = Field(default=None, max_length=500)
    paused_until: AwareDatetime | None = None

    @field_validator("excluded_apps")
    @classmethod
    def validate_excluded_apps(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = sorted({item.strip() for item in value if item.strip()}, key=str.casefold)
        if any(len(item) > 255 for item in cleaned):
            raise ValueError("excluded app identifiers must be at most 255 characters")
        return cleaned

    @field_validator("paused_until", mode="after")
    @classmethod
    def normalize_paused_until(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


class ActivityCollectionStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: ActivityPlatform | None = None
    capability: ActivityCapability | None = None
    permission_status: ActivityPermissionStatus | None = None
    status_reason: str | None = Field(default=None, max_length=255)
    last_collected_at: AwareDatetime | None = None
    last_uploaded_at: AwareDatetime | None = None
    queue_oldest_at: AwareDatetime | None = None
    queue_depth: int | None = Field(default=None, ge=0)
    coverage: float | None = Field(default=None, ge=0, le=1)

    @field_validator(
        "last_collected_at",
        "last_uploaded_at",
        "queue_oldest_at",
        mode="after",
    )
    @classmethod
    def normalize_status_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_platform_capability(self) -> ActivityCollectionStatusUpdate:
        if self.platform is ActivityPlatform.IOS and self.capability is ActivityCapability.DETAILED:
            raise ValueError("iOS detailed app timelines are not an MVP capability")
        return self


class ActivityPauseRequest(BaseModel):
    until: AwareDatetime

    @field_validator("until", mode="after")
    @classmethod
    def normalize_until(cls, value: datetime) -> datetime:
        return _utc(value)


class ActivityCollectionOut(BaseModel):
    device_id: str
    platform: ActivityPlatform
    enabled: bool
    excluded_apps: list[str]
    paused_until: datetime | None
    effective_collecting: bool
    blocked_reason: str | None
    permission_status: ActivityPermissionStatus
    capability: ActivityCapability
    status_reason: str | None
    last_collected_at: datetime | None
    last_uploaded_at: datetime | None
    queue_oldest_at: datetime | None
    queue_age_seconds: int | None
    queue_depth: int
    coverage: float | None
    config_revision: int
    cursors: dict[str, str]


class IOSAggregateSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_id: str = Field(min_length=1, max_length=255)
    bucket_start: AwareDatetime
    foreground_seconds: int = Field(ge=0, le=3600)
    category: str = Field(min_length=1, max_length=64)
    launches: int = Field(default=0, ge=0, le=100_000)
    opaque_app_token: str | None = Field(default=None, max_length=255)
    coverage_seconds: int | None = Field(default=None, ge=0, le=3600)

    @field_validator("bucket_start", mode="after")
    @classmethod
    def normalize_bucket_start(cls, value: datetime) -> datetime:
        return _utc(value)


class IOSCapabilityReport(BaseModel):
    """OS-honest iOS report: aggregate data or an explicit unavailable state."""

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=255)
    timezone: str = Field(min_length=1, max_length=64)
    capability: ActivityCapability
    permission_status: ActivityPermissionStatus
    reason: str | None = Field(default=None, max_length=255)
    collected_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    collection_revision: int | None = Field(default=None, ge=0)
    samples: list[IOSAggregateSample] = Field(default_factory=list, max_length=5000)

    @field_validator("timezone")
    @classmethod
    def validate_report_timezone(cls, value: str) -> str:
        return validate_timezone(value)

    @field_validator("collected_at", mode="after")
    @classmethod
    def normalize_collected_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_report(self) -> IOSCapabilityReport:
        if self.capability is ActivityCapability.DETAILED:
            raise ValueError("iOS detailed app timelines are not an MVP capability")
        available = (
            self.capability is ActivityCapability.AGGREGATE
            and self.permission_status is ActivityPermissionStatus.GRANTED
        )
        if not available and self.samples:
            raise ValueError("unavailable or denied iOS reports must not contain samples")
        if self.samples and self.collection_revision is None:
            raise ValueError("iOS reports with samples require collection_revision")
        return self


class ActivityWatchImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=255)
    platform: Literal[
        ActivityPlatform.MACOS,
        ActivityPlatform.WINDOWS,
        ActivityPlatform.LINUX,
    ]
    timezone: str = Field(min_length=1, max_length=64)
    base_url: HttpUrl = "http://127.0.0.1:5600"
    window_bucket_id: str | None = Field(default=None, max_length=255)
    afk_bucket_id: str | None = Field(default=None, max_length=255)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @field_validator("timezone")
    @classmethod
    def validate_import_timezone(cls, value: str) -> str:
        return validate_timezone(value)

    @field_validator("start_at", "end_at", mode="after")
    @classmethod
    def normalize_import_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_range(self) -> ActivityWatchImportRequest:
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("start_at and end_at must be provided together")
        if self.start_at is not None and self.end_at is not None and self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.end_at - self.start_at > timedelta(days=7)
        ):
            raise ValueError("one ActivityWatch import cannot exceed 7 days")
        return self


class ActivityContextResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_kind: Literal[
        "activity_summary",
        "focus",
        "overwork",
        "recovery",
        "caffeine_for_focus",
    ]
    date: str | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    lookback_days: int = Field(default=7, ge=1, le=90)
    nutrition_request_id: uuid.UUID | None = None
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("date")
    @classmethod
    def validate_optional_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be ISO YYYY-MM-DD") from exc
        return value

    @field_validator("timezone")
    @classmethod
    def validate_optional_timezone(cls, value: str | None) -> str | None:
        return validate_timezone(value) if value is not None else None

    @field_validator("start", "end", mode="after")
    @classmethod
    def normalize_context_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_context_range(self) -> ActivityContextResolveRequest:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        if (
            self.start is not None
            and self.end is not None
            and self.end - self.start > timedelta(days=1)
        ):
            raise ValueError("one activity context window cannot exceed 24 hours")
        return self


class ActivityDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str | None = Field(default=None, max_length=255)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    include_summaries: bool = True
    include_control: bool = False
    confirm: Literal[True]

    @field_validator("start", "end", mode="after")
    @classmethod
    def normalize_delete_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_delete_range(self) -> ActivityDeleteRequest:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class ActivityMaintenanceOut(BaseModel):
    expired_events_deleted: int
    compatibility_rows_deleted: int
    affected_dates: list[str]


JsonObject = dict[str, Any]
