"""Device-neutral contracts for phone and computer activity telemetry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from re import Pattern
from re import compile as compile_pattern
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from healthmes.timezones import parse_timezone

RESERVED_ACTIVITY_PROVIDER_NAMES = frozenset(
    {"activitywatch", "android-usage", "ios-device-activity"}
)
IOS_APP_TOKEN_PATTERN = (
    r"^ios-app-v2-(?P<key_fingerprint>[0-9a-f]{40})-"
    r"(?P<app_digest>[0-9a-f]{40})$"
)
IOS_APP_TOKEN_RE: Pattern[str] = compile_pattern(IOS_APP_TOKEN_PATTERN)
IOS_KEY_ID_PATTERN = r"^ios-key-[0-9a-f]{40}$"
IOS_CATEGORY_TOKEN_PATTERN = r"^ios-category-[0-9a-f]{40}$"
IOS_CATEGORY_TOKEN_RE: Pattern[str] = compile_pattern(
    IOS_CATEGORY_TOKEN_PATTERN
)
IOS_CANONICAL_CATEGORIES = frozenset(
    {
        "education",
        "entertainment",
        "finance",
        "fitness",
        "game",
        "news",
        "other",
        "productivity",
        "research",
        "shopping",
        "social",
        "travel",
        "utilities",
        "video",
    }
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def is_reserved_activity_provider(value: str) -> bool:
    normalized = value.casefold()
    return (
        normalized in RESERVED_ACTIVITY_PROVIDER_NAMES
        or normalized.startswith("healthmes-activity-")
    )


def is_ios_app_token(value: str) -> bool:
    return IOS_APP_TOKEN_RE.fullmatch(value) is not None


def ios_app_token_key_id(value: str) -> str | None:
    match = IOS_APP_TOKEN_RE.fullmatch(value)
    if match is None:
        return None
    return f"ios-key-{match.group('key_fingerprint')}"


def validate_timezone(value: str) -> str:
    try:
        parse_timezone(value)
    except ValueError as exc:
        raise ValueError(
            "timezone must be a valid IANA name or UTC offset, "
            f"got {value!r}"
        ) from exc
    return value


def _is_local_hour_boundary(
    value: datetime,
    zone: tzinfo,
) -> bool:
    instant = _utc(value)
    local = instant.astimezone(zone)
    if local.second != 0 or local.microsecond != 0:
        return False
    if local.minute == 0:
        return True

    next_local = (instant + timedelta(hours=1)).astimezone(zone)
    current_offset = local.utcoffset()
    next_offset = next_local.utcoffset()
    if current_offset is None or next_offset is None:
        return False
    shift_seconds = abs(
        int((next_offset - current_offset).total_seconds())
    )
    return (
        0 < shift_seconds < 3_600
        and shift_seconds % 60 == 0
        and local.minute == shift_seconds // 60
    )


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
    RESTRICTED = "restricted"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class IOSCoverageStatus(StrEnum):
    COMPLETE = "complete"
    PRIVACY_FILTERED = "privacy_filtered"
    WEBSITE_ACTIVITY = "website_activity"
    UNKNOWN_ACTIVITY = "unknown_activity"
    MIXED_PARTIAL = "mixed_partial"


def _validate_ios_coverage_marker(
    *,
    coverage_seconds: int | None,
    coverage_status: IOSCoverageStatus | None,
    observed_activity_seconds: int | None,
    represented_app_seconds: int | None,
    privacy_filtered_seconds: int | None,
    website_activity_seconds: int | None,
    unknown_activity_seconds: int | None,
) -> None:
    metadata = (
        observed_activity_seconds,
        represented_app_seconds,
        privacy_filtered_seconds,
        website_activity_seconds,
        unknown_activity_seconds,
    )
    if coverage_status is None:
        if coverage_seconds and all(value is None for value in metadata):
            # Backwards-compatible complete-zero marker.
            return
        raise ValueError(
            "coverage metadata requires coverage_status"
        )

    if any(value is None for value in metadata):
        raise ValueError(
            "coverage_status requires every coverage component"
        )
    assert observed_activity_seconds is not None
    assert represented_app_seconds is not None
    assert privacy_filtered_seconds is not None
    assert website_activity_seconds is not None
    assert unknown_activity_seconds is not None
    observed = observed_activity_seconds
    represented = represented_app_seconds
    private = privacy_filtered_seconds
    website = website_activity_seconds
    unknown = unknown_activity_seconds
    if represented + private + website + unknown != observed:
        raise ValueError(
            "coverage components must exactly partition observed activity"
        )

    partial_kinds = sum(value > 0 for value in (private, website, unknown))
    if coverage_status is IOSCoverageStatus.COMPLETE:
        if not coverage_seconds:
            raise ValueError(
                "complete coverage-only activity hours require positive coverage"
            )
        if partial_kinds:
            raise ValueError(
                "complete coverage cannot carry partial-coverage reasons"
            )
        if observed != 0 or represented != 0:
            raise ValueError(
                "complete coverage-only activity hours must represent zero usage"
            )
        return

    if coverage_seconds is not None:
        raise ValueError(
            "partial coverage-only activity hours cannot claim full coverage"
        )
    if observed <= 0 or partial_kinds == 0:
        raise ValueError(
            "partial coverage-only activity hours require observed partial activity"
        )
    if (
        coverage_status is IOSCoverageStatus.PRIVACY_FILTERED
        and not (private > 0 and partial_kinds == 1)
    ):
        raise ValueError("privacy_filtered coverage requires only private activity")
    if (
        coverage_status is IOSCoverageStatus.WEBSITE_ACTIVITY
        and not (website > 0 and partial_kinds == 1)
    ):
        raise ValueError("website_activity coverage requires only website activity")
    if (
        coverage_status is IOSCoverageStatus.UNKNOWN_ACTIVITY
        and not (unknown > 0 and partial_kinds == 1)
    ):
        raise ValueError("unknown_activity coverage requires only unknown activity")
    if (
        coverage_status is IOSCoverageStatus.MIXED_PARTIAL
        and partial_kinds < 2
    ):
        raise ValueError(
            "mixed_partial coverage requires multiple partial-coverage reasons"
        )


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
    launches_observed: bool = True
    category: str | None = Field(default=None, max_length=64)
    coverage_seconds: int | None = Field(default=None, ge=0, le=3600)
    coverage_only: bool = False
    coverage_status: IOSCoverageStatus | None = None
    observed_activity_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    represented_app_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    privacy_filtered_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    website_activity_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    unknown_activity_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    bucket_complete: bool = True
    snapshot_sequence: int | None = Field(
        default=None,
        ge=0,
        le=2**63 - 1,
    )

    @field_validator("bucket_start", mode="after")
    @classmethod
    def normalize_bucket_start(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_coverage_only(self) -> AppHourRecord:
        if not self.coverage_only:
            if any(
                value is not None
                for value in (
                    self.coverage_status,
                    self.observed_activity_seconds,
                    self.represented_app_seconds,
                    self.privacy_filtered_seconds,
                    self.website_activity_seconds,
                    self.unknown_activity_seconds,
                )
            ):
                raise ValueError(
                    "iOS coverage metadata requires a coverage-only record"
                )
            return self
        if self.app_id != "__healthmes_coverage__":
            raise ValueError(
                "coverage-only activity hours require the reserved app_id"
            )
        if self.foreground_seconds != 0 or self.launches != 0:
            raise ValueError(
                "coverage-only activity hours cannot carry activity counts"
            )
        if self.launches_observed:
            raise ValueError(
                "coverage-only activity hours cannot claim observed launches"
            )
        if self.category is not None:
            raise ValueError(
                "coverage-only activity hours cannot carry a category"
            )
        _validate_ios_coverage_marker(
            coverage_seconds=self.coverage_seconds,
            coverage_status=self.coverage_status,
            observed_activity_seconds=self.observed_activity_seconds,
            represented_app_seconds=self.represented_app_seconds,
            privacy_filtered_seconds=self.privacy_filtered_seconds,
            website_activity_seconds=self.website_activity_seconds,
            unknown_activity_seconds=self.unknown_activity_seconds,
        )
        return self


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
        cleaned = sorted(
            {item.strip() for item in value if item.strip()},
            key=lambda item: (item.casefold(), item),
        )
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
    status_observed_at: AwareDatetime | None = None
    collection_generation: int | None = Field(
        default=None,
        ge=0,
        le=2**63 - 1,
    )
    pairing_revision: int | None = Field(
        default=None,
        ge=0,
        le=2**63 - 1,
    )
    last_collected_at: AwareDatetime | None = None
    last_uploaded_at: AwareDatetime | None = None
    queue_oldest_at: AwareDatetime | None = None
    queue_depth: int | None = Field(default=None, ge=0)
    coverage: float | None = Field(default=None, ge=0, le=1)

    @field_validator(
        "last_collected_at",
        "last_uploaded_at",
        "queue_oldest_at",
        "status_observed_at",
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
    status_observed_at: datetime | None
    collection_generation: int | None
    last_collected_at: datetime | None
    last_uploaded_at: datetime | None
    queue_oldest_at: datetime | None
    queue_age_seconds: int | None
    queue_depth: int
    coverage: float | None
    config_revision: int
    cursors: dict[str, str]
    ios_pseudonym_key_id: str | None = Field(
        default=None,
        pattern=IOS_KEY_ID_PATTERN,
    )
    raw_retention_cutoff: datetime | None = None


class IOSAggregateSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_id: str = Field(min_length=1, max_length=255)
    bucket_start: AwareDatetime
    foreground_seconds: int = Field(ge=0, le=3600)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    # Screen Time exposes pickups, not app-launch counts. A missing value must
    # remain unknown rather than becoming a fake observed zero.
    launches: int | None = Field(default=None, ge=0, le=100_000)
    opaque_app_token: str | None = Field(
        default=None,
        pattern=IOS_APP_TOKEN_PATTERN,
    )
    coverage_seconds: int | None = Field(default=None, ge=0, le=3600)
    coverage_only: bool = False
    coverage_status: IOSCoverageStatus | None = None
    observed_activity_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    represented_app_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    privacy_filtered_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    website_activity_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )
    unknown_activity_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
    )

    @field_validator("bucket_start", mode="after")
    @classmethod
    def normalize_bucket_start(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("launches")
    @classmethod
    def reject_unobservable_launches(
        cls,
        value: int | None,
    ) -> int | None:
        if value is not None:
            raise ValueError(
                "iOS Screen Time reports cannot provide app launch counts"
            )
        return value

    @field_validator("category")
    @classmethod
    def validate_privacy_safe_category(
        cls,
        value: str | None,
    ) -> str | None:
        if (
            value is not None
            and value not in IOS_CANONICAL_CATEGORIES
            and IOS_CATEGORY_TOKEN_RE.fullmatch(value) is None
        ):
            raise ValueError(
                "iOS category must be canonical or an opaque "
                "ios-category token"
            )
        return value

    @model_validator(mode="after")
    def validate_coverage_only(self) -> IOSAggregateSample:
        if self.coverage_only:
            if self.foreground_seconds != 0 or self.launches is not None:
                raise ValueError(
                    "coverage-only iOS samples cannot carry activity counts"
                )
            if self.opaque_app_token is not None or self.category is not None:
                raise ValueError(
                    "coverage-only iOS samples cannot carry app or category identity"
                )
            _validate_ios_coverage_marker(
                coverage_seconds=self.coverage_seconds,
                coverage_status=self.coverage_status,
                observed_activity_seconds=self.observed_activity_seconds,
                represented_app_seconds=self.represented_app_seconds,
                privacy_filtered_seconds=self.privacy_filtered_seconds,
                website_activity_seconds=self.website_activity_seconds,
                unknown_activity_seconds=self.unknown_activity_seconds,
            )
        else:
            if self.opaque_app_token is None:
                raise ValueError(
                    "iOS activity samples require opaque_app_token"
                )
            if self.category is None:
                raise ValueError("iOS activity samples require a category")
            if any(
                value is not None
                for value in (
                    self.coverage_status,
                    self.observed_activity_seconds,
                    self.represented_app_seconds,
                    self.privacy_filtered_seconds,
                    self.website_activity_seconds,
                    self.unknown_activity_seconds,
                )
            ):
                raise ValueError(
                    "iOS coverage metadata requires a coverage-only sample"
                )
        return self


class IOSCapabilityReport(BaseModel):
    """OS-honest iOS report: aggregate data or an explicit unavailable state."""

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=255)
    timezone: str = Field(min_length=1, max_length=64)
    capability: ActivityCapability
    permission_status: ActivityPermissionStatus
    pseudonym_key_id: str | None = Field(
        default=None,
        pattern=IOS_KEY_ID_PATTERN,
    )
    reason: str | None = Field(default=None, max_length=255)
    collected_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    collection_revision: int | None = Field(default=None, ge=0)
    collection_generation: int | None = Field(
        default=None,
        ge=0,
        le=2**63 - 1,
    )
    reset_snapshot_fence: bool = False
    snapshot_sequence: int | None = Field(
        default=None,
        ge=1,
        le=2**63 - 1,
    )
    snapshot_start: AwareDatetime | None = None
    snapshot_end: AwareDatetime | None = None
    authoritative_bucket_starts: list[AwareDatetime] = Field(
        default_factory=list,
        max_length=168,
        description=(
            "Post-privacy replacement scopes. For each listed hour, the "
            "server removes prior iOS rows missing from this report. A "
            "privacy-filtered hour can therefore be authoritative while its "
            "remaining samples keep coverage_seconds unset."
        ),
    )
    samples: list[IOSAggregateSample] = Field(default_factory=list, max_length=5000)

    @field_validator("timezone")
    @classmethod
    def validate_report_timezone(cls, value: str) -> str:
        return validate_timezone(value)

    @field_validator(
        "collected_at",
        "snapshot_start",
        "snapshot_end",
        mode="after",
    )
    @classmethod
    def normalize_report_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return _utc(value) if value is not None else None

    @field_validator("authoritative_bucket_starts", mode="after")
    @classmethod
    def normalize_authoritative_bucket_starts(
        cls,
        value: list[datetime],
    ) -> list[datetime]:
        normalized = sorted({_utc(item) for item in value})
        if len(normalized) != len(value):
            raise ValueError(
                "authoritative iOS bucket starts must be unique"
            )
        return normalized

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
        if not available and self.authoritative_bucket_starts:
            raise ValueError(
                "unavailable or denied iOS reports cannot claim authoritative buckets"
            )
        snapshot_fields = (
            self.snapshot_sequence,
            self.snapshot_start,
            self.snapshot_end,
        )
        authoritative = all(value is not None for value in snapshot_fields)
        if (
            available
            and (self.samples or authoritative)
            and self.pseudonym_key_id is None
        ):
            raise ValueError(
                "granted iOS aggregate reports require pseudonym_key_id"
            )
        if any(value is not None for value in snapshot_fields) and not authoritative:
            raise ValueError(
                "iOS authoritative reports require snapshot_sequence, "
                "snapshot_start, and snapshot_end together"
            )
        if authoritative and not available:
            raise ValueError(
                "only granted iOS aggregate reports may carry snapshots"
            )
        if self.samples and len(
            {sample.source_record_id for sample in self.samples}
        ) != len(self.samples):
            raise ValueError(
                "iOS snapshot samples must contain unique source_record_id values"
            )
        samples_by_bucket: dict[datetime, list[IOSAggregateSample]] = {}
        for sample in self.samples:
            samples_by_bucket.setdefault(sample.bucket_start, []).append(
                sample
            )
        for bucket_samples in samples_by_bucket.values():
            coverage_markers = [
                sample for sample in bucket_samples if sample.coverage_only
            ]
            if len(coverage_markers) > 1:
                raise ValueError(
                    "one iOS bucket may contain at most one coverage marker"
                )
            if not coverage_markers:
                continue
            marker = coverage_markers[0]
            app_samples = [
                sample
                for sample in bucket_samples
                if not sample.coverage_only
            ]
            represented = sum(
                sample.foreground_seconds for sample in app_samples
            )
            if marker.represented_app_seconds is None:
                if represented:
                    raise ValueError(
                        "legacy complete coverage markers cannot accompany app activity"
                    )
            elif marker.represented_app_seconds != represented:
                raise ValueError(
                    "coverage represented_app_seconds must equal bucket app activity"
                )
            if (
                marker.coverage_status is not IOSCoverageStatus.COMPLETE
                and any(
                    sample.coverage_seconds is not None
                    for sample in app_samples
                )
            ):
                raise ValueError(
                    "partial-coverage bucket app samples cannot claim full coverage"
                )
        token_key_ids = {
            key_id
            for sample in self.samples
            if sample.opaque_app_token is not None
            for key_id in (ios_app_token_key_id(sample.opaque_app_token),)
            if key_id is not None
        }
        if len(token_key_ids) > 1:
            raise ValueError(
                "one iOS snapshot cannot mix app pseudonym key namespaces"
            )
        if token_key_ids and token_key_ids != {self.pseudonym_key_id}:
            raise ValueError(
                "iOS app tokens must match the report pseudonym_key_id"
            )
        if self.samples and self.collection_revision is None:
            raise ValueError("iOS reports with samples require collection_revision")
        if authoritative and self.collection_revision is None:
            raise ValueError(
                "iOS authoritative reports require collection_revision"
            )
        if self.reset_snapshot_fence and not authoritative:
            raise ValueError(
                "iOS snapshot fence reset requires an authoritative report"
            )
        if self.reset_snapshot_fence and self.collection_generation is None:
            raise ValueError(
                "iOS snapshot fence reset requires collection_generation"
            )
        if authoritative:
            start = self.snapshot_start
            end = self.snapshot_end
            assert start is not None and end is not None
            if start >= end:
                raise ValueError("snapshot_start must be before snapshot_end")
            if end - start > timedelta(days=7):
                raise ValueError("one iOS snapshot cannot exceed 7 days")
            zone = parse_timezone(self.timezone)
            if any(
                not _is_local_hour_boundary(value, zone)
                for value in (
                    start,
                    end,
                )
            ):
                raise ValueError(
                    "iOS snapshot bounds must align to local clock hours"
                )
            authoritative_buckets = set(
                self.authoritative_bucket_starts
            )
            if any(
                value < start or value >= end
                for value in authoritative_buckets
            ):
                raise ValueError(
                    "authoritative iOS buckets must be contained in the "
                    "snapshot range"
                )
            if any(
                sample.bucket_start < start
                or sample.bucket_start + timedelta(hours=1) > end
                for sample in self.samples
            ):
                raise ValueError(
                    "every iOS sample must be fully contained in its "
                    "authoritative snapshot range"
                )
            if any(
                sample.bucket_start not in authoritative_buckets
                for sample in self.samples
            ):
                raise ValueError(
                    "every iOS sample must belong to an explicitly "
                    "authoritative bucket"
                )
        zone = parse_timezone(self.timezone)
        if any(
            not _is_local_hour_boundary(
                sample.bucket_start,
                zone,
            )
            for sample in self.samples
        ):
            raise ValueError(
                "iOS sample buckets must align to local clock hours"
            )
        if any(
            not _is_local_hour_boundary(
                value,
                zone,
            )
            for value in self.authoritative_bucket_starts
        ):
            raise ValueError(
                "authoritative iOS buckets must align to local clock hours"
            )
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
