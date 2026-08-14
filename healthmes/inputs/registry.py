"""Compose existing source controls into one UI-facing input registry."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.contracts import (
    ActivityCollectionUpdate,
    ActivityPlatform,
    ios_app_token_key_id,
    is_ios_app_token,
)
from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.activity.repository import (
    COLLECTION_CONFIG_EVENT,
    COLLECTION_CONTROL_EVENT,
    COLLECTION_STATUS_EVENT,
    CONTROL_PROVIDER,
    InvalidIOSAppTokenError,
    get_control_payload,
    serialize_collection_state,
    update_collection_config,
)
from healthmes.calendars import creds
from healthmes.config import Settings
from healthmes.decision import (
    list_decision_domain_policies,
    update_decision_domain_policy,
)
from healthmes.inputs.contracts import (
    InputActionDescriptor,
    InputCollectionState,
    InputConnectionState,
    InputInstance,
    InputPrivacyProfile,
    InputRetentionPolicy,
    InputSettingDefinition,
    InputSettingsUpdate,
    InputSourceDescriptor,
)
from healthmes.storage import (
    RETENTION_PRESETS,
    ensure_default_policies,
    update_retention_policy,
)
from healthmes.store import (
    RawIngestEvent,
    RetentionPolicy,
    WellnessEvent,
)
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
)

_RETENTION_PRESET_BY_DAYS = {
    days: preset for preset, days in RETENTION_PRESETS.items()
}


@dataclass(frozen=True, slots=True)
class _SourceSpec:
    source_id: str
    domain: str
    display_name: str
    platforms: tuple[str, ...]
    capabilities: tuple[str, ...]
    retention_classes: tuple[str, ...]
    privacy: InputPrivacyProfile
    activity_platforms: tuple[ActivityPlatform, ...] = ()
    supports_collection_settings: bool = False
    supports_exclusions: bool = False
    actions: tuple[InputActionDescriptor, ...] = ()
    limitations: tuple[str, ...] = ()


_DECISION_SETTING = InputSettingDefinition(
    key="decision_access_enabled",
    value_type="boolean",
    scope="domain",
    description=(
        "Allow the HealthMes Decision Agent to query this domain through "
        "the Context Access Layer."
    ),
)
_RETENTION_SETTING = InputSettingDefinition(
    key="retention",
    value_type="retention_map",
    scope="data_class",
    allowed_values=list(RETENTION_PRESETS),
    description=(
        "Set 1, 7, 14, 30, 90 day, or indefinite retention for the "
        "source's existing storage classes."
    ),
)
_INSTANCE_SETTINGS = (
    InputSettingDefinition(
        key="enabled",
        value_type="boolean",
        scope="instance",
        description="Enable or disable collection for one device instance.",
    ),
    InputSettingDefinition(
        key="paused_until",
        value_type="datetime",
        scope="instance",
        description="Pause collection until an absolute UTC timestamp.",
    ),
)
_EXCLUSION_SETTING = InputSettingDefinition(
    key="excluded_apps",
    value_type="string_list",
    scope="instance",
    description=(
        "App identifiers excluded from future activity. Android uses "
        "UsageStats package names, ActivityWatch uses window event "
        "data.app values, and iPhone uses device-keyed "
        "ios-app-v2-* tokens."
    ),
)

_SOURCES = (
    _SourceSpec(
        source_id="activity.android",
        domain="activity",
        display_name="Android app activity",
        platforms=("android",),
        capabilities=("hourly_app_usage", "hourly_category_usage"),
        retention_classes=(
            "activity_raw",
            "activity_hourly",
            "activity_daily",
        ),
        activity_platforms=(ActivityPlatform.ANDROID,),
        supports_collection_settings=True,
        supports_exclusions=True,
        actions=(
            InputActionDescriptor(
                action="authorize",
                execution="device",
                requires_instance=True,
                description="Open Android Usage Access authorization.",
            ),
            InputActionDescriptor(
                action="sync",
                execution="device",
                requires_instance=True,
                description="Run the Android collector and upload its queue.",
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=False,
            source_side_exclusions=True,
            default_llm_exposure="aggregate_only",
            notes=(
                "No screenshots, keystrokes, URLs, or app content are collected.",
            ),
        ),
    ),
    _SourceSpec(
        source_id="activity.activitywatch",
        domain="activity",
        display_name="Desktop ActivityWatch",
        platforms=("macos", "windows", "linux"),
        capabilities=(
            "foreground_app_intervals",
            "idle_intervals",
            "hourly_activity_summary",
        ),
        retention_classes=(
            "activity_raw",
            "activity_hourly",
            "activity_daily",
        ),
        activity_platforms=(
            ActivityPlatform.MACOS,
            ActivityPlatform.WINDOWS,
            ActivityPlatform.LINUX,
        ),
        supports_collection_settings=True,
        supports_exclusions=True,
        actions=(
            InputActionDescriptor(
                action="connect",
                execution="local_cli",
                description=(
                    "Configure the loopback ActivityWatch endpoint in the "
                    "HealthMes server environment."
                ),
            ),
            InputActionDescriptor(
                action="sync",
                execution="server",
                method="POST",
                endpoint="/v1/activity/activitywatch/import",
                requires_instance=True,
                description="Run one bounded ActivityWatch import.",
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=False,
            source_side_exclusions=True,
            default_llm_exposure="aggregate_only",
            notes=(
                "Window titles and URLs are discarded by the adapter and are "
                "never persisted or exposed to the Decision Agent.",
            ),
        ),
    ),
    _SourceSpec(
        source_id="activity.ios-screentime",
        domain="activity",
        display_name="iPhone Screen Time",
        platforms=("ios",),
        capabilities=("hourly_app_usage", "hourly_category_usage"),
        retention_classes=(
            "activity_raw",
            "activity_hourly",
            "activity_daily",
        ),
        activity_platforms=(ActivityPlatform.IOS,),
        supports_collection_settings=True,
        supports_exclusions=True,
        actions=(
            InputActionDescriptor(
                action="authorize",
                execution="device",
                requires_instance=True,
                description=(
                    "Unavailable in normal repository builds. A gate-enabled, "
                    "entitled iPhone build may request Apple's App & Website "
                    "Usage data authorization."
                ),
            ),
            InputActionDescriptor(
                action="sync",
                execution="device",
                requires_instance=True,
                description=(
                    "Unavailable in normal repository builds. A gate-enabled, "
                    "entitled build with lifecycle wiring may upload completed "
                    "Screen Time hours as an authoritative snapshot."
                ),
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=False,
            source_side_exclusions=True,
            default_llm_exposure="aggregate_only",
            notes=(
                "App identifiers are pseudonymized on the phone.",
                "No screenshots, taps, keystrokes, URLs, notifications, or "
                "pickups are uploaded.",
            ),
        ),
        limitations=(
            "ios_screen_time_normal_build_unavailable",
            "ios_screen_time_export_requires_ios_26_4",
            "ios_screen_time_export_requires_apple_entitlement",
            "ios_screen_time_export_customer_access_is_eu_limited",
            "ios_screen_time_lifecycle_not_wired",
            "ios_screen_time_background_task_not_registered",
            "ios_screen_time_not_real_device_verified",
        ),
    ),
    _SourceSpec(
        source_id="nutrition.capture",
        domain="nutrition",
        display_name="Nutrition capture",
        platforms=("ios", "android", "macos", "windows", "web"),
        capabilities=(
            "photo_vlm",
            "free_text",
            "voice_transcript",
            "structured_nutrients",
            "caffeine",
        ),
        retention_classes=(
            "nutrition_media",
            "nutrition_raw_capture",
            "nutrition_observation",
            "nutrition_confirmation",
        ),
        actions=(
            InputActionDescriptor(
                action="capture",
                execution="device",
                description=(
                    "Submit a photo, text, voice transcript, or structured "
                    "nutrition observation."
                ),
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=True,
            source_side_exclusions=False,
            default_llm_exposure="structured_only",
            notes=(
                "Media and raw capture retention are independently configurable.",
            ),
        ),
    ),
    _SourceSpec(
        source_id="wearable.healthkit-bridge",
        domain="wearable",
        display_name="Apple HealthKit bridge",
        platforms=("ios", "watchos", "server"),
        capabilities=(
            "raw_first_ingest",
            "healthkit_auto_export",
            "open_wearables_forwarding",
        ),
        retention_classes=("raw_payload",),
        actions=(
            InputActionDescriptor(
                action="connect",
                execution="external",
                description=(
                    "Configure a HealthKit auto-export app to send data to "
                    "the HealthMes receiver."
                ),
            ),
            InputActionDescriptor(
                action="sync",
                execution="device",
                method="POST",
                endpoint="/v1/ingest/healthkit",
                description=(
                    "External exporters POST HealthKit payloads to this "
                    "raw-first receiver."
                ),
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=True,
            source_side_exclusions=False,
            default_llm_exposure="none",
            notes=(
                "Raw HealthKit payloads are retained under the raw_payload "
                "policy before best-effort normalization.",
                "The Decision Agent receives only separately normalized "
                "wearable context, never this raw payload by default.",
            ),
        ),
        limitations=(
            "healthkit_exporter_configuration_is_external",
            "healthkit_delivery_freshness_is_not_observed",
        ),
    ),
    _SourceSpec(
        source_id="wearable.open-wearables",
        domain="wearable",
        display_name="Open Wearables",
        platforms=("server",),
        capabilities=(
            "sleep",
            "recovery",
            "hrv",
            "stress",
            "workouts",
        ),
        retention_classes=(OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,),
        actions=(
            InputActionDescriptor(
                action="connect",
                execution="external",
                description=(
                    "Connect wearable providers in the Open Wearables service."
                ),
            ),
            InputActionDescriptor(
                action="sync",
                execution="external",
                description="Run provider synchronization in Open Wearables.",
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=False,
            source_side_exclusions=False,
            default_llm_exposure="aggregate_only",
            notes=(
                "HealthMes stores normalized provenance snapshots, not provider "
                "credentials.",
            ),
        ),
    ),
    _SourceSpec(
        source_id="calendar.google",
        domain="calendar",
        display_name="Google Calendar",
        platforms=("server", "web"),
        capabilities=("event_mirror", "availability", "schedule_density"),
        retention_classes=("calendar_mirror",),
        actions=(
            InputActionDescriptor(
                action="connect",
                execution="browser",
                method="POST",
                endpoint="/connect/google/start",
                description="Start the existing Google OAuth connection flow.",
            ),
            InputActionDescriptor(
                action="disconnect",
                execution="browser",
                method="POST",
                endpoint="/connect/google/disconnect",
                description="Disconnect the current Google Calendar account.",
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=True,
            source_side_exclusions=False,
            default_llm_exposure="aggregate_only",
            notes=(
                "Calendar titles and descriptions are not sent to the LLM "
                "unless a capability explicitly requires them.",
            ),
        ),
    ),
    _SourceSpec(
        source_id="calendar.icloud",
        domain="calendar",
        display_name="iCloud Calendar",
        platforms=("server", "ios", "macos"),
        capabilities=("event_mirror", "availability", "schedule_density"),
        retention_classes=("calendar_mirror",),
        actions=(
            InputActionDescriptor(
                action="connect",
                execution="local_cli",
                description=(
                    "Store an iCloud app password with `healthmes connect "
                    "icloud`."
                ),
            ),
            InputActionDescriptor(
                action="disconnect",
                execution="local_cli",
                description="Remove the stored iCloud CalDAV credential.",
            ),
        ),
        privacy=InputPrivacyProfile(
            local_first=True,
            raw_content_collected=True,
            source_side_exclusions=False,
            default_llm_exposure="aggregate_only",
            notes=(
                "Calendar credentials are stored outside wellness records.",
            ),
        ),
    ),
)
_SOURCE_BY_ID = {source.source_id: source for source in _SOURCES}


class InputSourceRegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InputSourceRegistry:
    """Read and mutate existing source controls through one stable contract."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings

    def list(self, session: Session) -> tuple[InputSourceDescriptor, ...]:
        policies = {
            row.data_class: row
            for row in ensure_default_policies(session)
        }
        decision_rows = {
            row.domain: row
            for row in list_decision_domain_policies(
                session,
                self._settings.decision_owner_principal_id,
            )
        }
        activity = self._activity_instances(session)
        raw_ingest_sources = set(
            session.scalars(select(RawIngestEvent.source).distinct())
        )
        session.commit()
        return tuple(
            self._descriptor(
                source,
                activity=activity,
                policies=policies,
                decision_rows=decision_rows,
                raw_ingest_sources=raw_ingest_sources,
            )
            for source in _SOURCES
        )

    def get(
        self,
        session: Session,
        source_id: str,
    ) -> InputSourceDescriptor:
        source = self._source(source_id)
        return next(
            descriptor
            for descriptor in self.list(session)
            if descriptor.source_id == source.source_id
        )

    def update(
        self,
        session: Session,
        source_id: str,
        update: InputSettingsUpdate,
    ) -> InputSourceDescriptor:
        source = self._source(source_id)
        collection_values = update.collection_values()
        collection_requested = bool(collection_values) or update.platform is not None
        if update.excluded_apps is not None and not source.supports_exclusions:
            raise InputSourceRegistryError(
                "input_exclusions_unsupported",
                f"{source.source_id} does not support app exclusions",
            )
        if collection_requested and not source.supports_collection_settings:
            raise InputSourceRegistryError(
                "input_collection_settings_unsupported",
                f"{source.source_id} does not expose per-device collection settings",
            )
        if collection_requested and update.instance_id is None:
            raise InputSourceRegistryError(
                "input_instance_required",
                "instance_id is required for collection settings",
            )
        invalid_retention = sorted(
            set(update.retention) - set(source.retention_classes)
        )
        if invalid_retention:
            raise InputSourceRegistryError(
                "input_retention_class_unsupported",
                "unsupported retention class(es): "
                + ", ".join(invalid_retention),
            )
        if (
            update.paused_until is not None
            and update.paused_until <= datetime.now(UTC)
        ):
            raise InputSourceRegistryError(
                "invalid_input_pause",
                "paused_until must be in the future or null",
            )

        with activity_write_lock():
            lock_activity_write_plane(session)
            if collection_requested:
                assert update.instance_id is not None
                if source.activity_platforms:
                    platform = self._activity_platform_for_update(
                        session,
                        source,
                        update.instance_id,
                        requested_platform=update.platform,
                    )
                    if (
                        platform is ActivityPlatform.IOS
                        and update.excluded_apps is not None
                        and (
                            any(
                                not is_ios_app_token(value)
                                for value in update.excluded_apps
                            )
                            or len(
                                {
                                    ios_app_token_key_id(value)
                                    for value in update.excluded_apps
                                }
                            )
                            > 1
                        )
                    ):
                        raise InputSourceRegistryError(
                            "invalid_ios_app_token",
                            "iOS excluded apps must be v2 tokens from one "
                            "device pseudonym key namespace",
                        )
                    try:
                        update_collection_config(
                            session,
                            update.instance_id,
                            ActivityCollectionUpdate(
                                platform=platform,
                                **collection_values,
                            ),
                        )
                    except InvalidIOSAppTokenError as exc:
                        raise InputSourceRegistryError(
                            "invalid_ios_app_token",
                            str(exc),
                        ) from exc
            for data_class, preset in sorted(update.retention.items()):
                update_retention_policy(session, data_class, preset)
            if update.decision_access_enabled is not None:
                update_decision_domain_policy(
                    session,
                    self._settings.decision_owner_principal_id,
                    source.domain,
                    enabled=update.decision_access_enabled,
                )
            session.commit()
        return self.get(session, source.source_id)

    def _source(self, source_id: str) -> _SourceSpec:
        normalized = source_id.strip().casefold()
        source = _SOURCE_BY_ID.get(normalized)
        if source is None:
            raise InputSourceRegistryError(
                "input_source_not_found",
                f"unknown input source: {source_id}",
            )
        return source

    def _activity_instances(
        self,
        session: Session,
    ) -> dict[ActivityPlatform, list[InputInstance]]:
        rows = session.execute(
            select(
                WellnessEvent.source_device,
                WellnessEvent.payload,
            ).where(
                WellnessEvent.event_type.in_(
                    (
                        COLLECTION_CONTROL_EVENT,
                        COLLECTION_CONFIG_EVENT,
                        COLLECTION_STATUS_EVENT,
                    )
                ),
                WellnessEvent.source_provider == CONTROL_PROVIDER,
            )
        )
        devices: dict[str, ActivityPlatform] = {}
        for source_device, payload in rows:
            if not isinstance(payload, dict):
                continue
            raw_id = payload.get("device_id") or source_device
            if not isinstance(raw_id, str) or not raw_id:
                continue
            raw_platform = payload.get("platform")
            try:
                platform = ActivityPlatform(str(raw_platform))
            except ValueError:
                platform = ActivityPlatform.UNKNOWN
            previous = devices.get(raw_id)
            if (
                previous is None
                or previous is ActivityPlatform.UNKNOWN
                or platform is not ActivityPlatform.UNKNOWN
            ):
                devices[raw_id] = platform

        if (
            self._settings.activitywatch_enabled
            and self._settings.activitywatch_device_id not in devices
        ):
            devices[self._settings.activitywatch_device_id] = ActivityPlatform(
                self._settings.activitywatch_platform
            )

        result: dict[ActivityPlatform, list[InputInstance]] = {}
        for device_id, platform in sorted(devices.items()):
            payload = get_control_payload(
                session,
                device_id,
                platform=platform,
            )
            serialized = serialize_collection_state(payload)
            actual_platform = ActivityPlatform(serialized["platform"])
            result.setdefault(actual_platform, []).append(
                InputInstance(
                    instance_id=device_id,
                    platform=actual_platform.value,
                    enabled=bool(serialized["enabled"]),
                    effective_collecting=(
                        bool(serialized["effective_collecting"])
                        and serialized["status_observed_at"] is not None
                    ),
                    permission_status=str(
                        serialized["permission_status"]
                    ),
                    capability=str(serialized["capability"]),
                    blocked_reason=serialized["blocked_reason"],
                    status_reason=serialized["status_reason"],
                    status_observed_at=serialized["status_observed_at"],
                    last_collected_at=serialized["last_collected_at"],
                    last_uploaded_at=serialized["last_uploaded_at"],
                    coverage=serialized["coverage"],
                    excluded_apps=list(serialized["excluded_apps"]),
                    paused_until=serialized["paused_until"],
                    config_revision=int(serialized["config_revision"]),
                )
            )
        return result

    def _activity_platform_for_update(
        self,
        session: Session,
        source: _SourceSpec,
        instance_id: str,
        *,
        requested_platform: str | None,
    ) -> ActivityPlatform:
        if not source.activity_platforms:
            raise InputSourceRegistryError(
                "input_collection_settings_unsupported",
                f"{source.source_id} is not an activity collector",
            )
        requested: ActivityPlatform | None = None
        if requested_platform is not None:
            try:
                requested = ActivityPlatform(requested_platform)
            except ValueError as exc:
                raise InputSourceRegistryError(
                    "input_platform_unsupported",
                    f"unsupported activity platform: {requested_platform}",
                ) from exc
            if requested not in source.activity_platforms:
                raise InputSourceRegistryError(
                    "input_platform_unsupported",
                    f"{requested.value} is not supported by {source.source_id}",
                )
        existing = get_control_payload(session, instance_id)
        try:
            existing_platform = ActivityPlatform(
                str(existing.get("platform", "unknown"))
            )
        except ValueError:
            existing_platform = ActivityPlatform.UNKNOWN
        if (
            existing_platform is ActivityPlatform.UNKNOWN
            and source.source_id == "activity.activitywatch"
            and instance_id == self._settings.activitywatch_device_id
        ):
            existing_platform = ActivityPlatform(
                self._settings.activitywatch_platform
            )
        if (
            existing_platform is not ActivityPlatform.UNKNOWN
            and existing_platform not in source.activity_platforms
        ):
            raise InputSourceRegistryError(
                "input_instance_source_mismatch",
                f"{instance_id} belongs to {existing_platform.value}, not "
                f"{source.source_id}",
            )
        if (
            requested is not None
            and existing_platform is not ActivityPlatform.UNKNOWN
            and requested is not existing_platform
        ):
            raise InputSourceRegistryError(
                "input_platform_conflict",
                f"{instance_id} is already registered as "
                f"{existing_platform.value}, not {requested.value}",
            )
        if requested is not None:
            return requested
        if len(source.activity_platforms) == 1:
            return source.activity_platforms[0]
        if existing_platform in source.activity_platforms:
            return existing_platform
        if (
            source.source_id == "activity.activitywatch"
            and instance_id == self._settings.activitywatch_device_id
        ):
            return ActivityPlatform(self._settings.activitywatch_platform)
        raise InputSourceRegistryError(
            "input_platform_required",
            "a desktop instance must report its platform before it can be "
            "managed through the unified input settings endpoint",
        )

    def _descriptor(
        self,
        source: _SourceSpec,
        *,
        activity: dict[ActivityPlatform, list[InputInstance]],
        policies: dict[str, RetentionPolicy],
        decision_rows: dict[str, Any],
        raw_ingest_sources: set[str],
    ) -> InputSourceDescriptor:
        instances = [
            instance
            for platform in source.activity_platforms
            for instance in activity.get(platform, ())
        ]
        connection, collection = self._states(
            source,
            instances,
            raw_ingest_sources=raw_ingest_sources,
        )
        decision = decision_rows.get(source.domain)
        settings = [_DECISION_SETTING]
        if source.retention_classes:
            settings.append(_RETENTION_SETTING)
        if source.supports_collection_settings:
            instance_settings = list(_INSTANCE_SETTINGS)
            if source.supports_exclusions:
                instance_settings.insert(1, _EXCLUSION_SETTING)
            settings = [*instance_settings, *settings]
        retention = []
        for data_class in source.retention_classes:
            row = policies.get(data_class)
            if row is None:
                continue
            preset = _RETENTION_PRESET_BY_DAYS.get(row.retention_days)
            if preset is None:
                continue
            retention.append(
                InputRetentionPolicy(
                    data_class=data_class,
                    preset=preset,
                    retention_days=row.retention_days,
                    enabled=bool(row.enabled),
                    effective_preset=(
                        preset if row.enabled else "disabled"
                    ),
                )
            )
        descriptor = InputSourceDescriptor(
            source_id=source.source_id,
            domain=source.domain,
            display_name=source.display_name,
            platforms=list(source.platforms),
            capabilities=list(source.capabilities),
            connection_state=connection,
            collection_state=collection,
            decision_access_enabled=bool(
                getattr(decision, "enabled", False)
            ),
            instances=instances,
            retention=retention,
            settings=settings,
            actions=list(source.actions),
            privacy=source.privacy,
            limitations=list(source.limitations),
            revision="sha256:" + ("0" * 64),
        )
        revision_payload = descriptor.model_dump(
            mode="json",
            exclude={"revision"},
        )
        digest = hashlib.sha256(
            json.dumps(
                revision_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        return descriptor.model_copy(
            update={"revision": f"sha256:{digest}"}
        )

    def _states(
        self,
        source: _SourceSpec,
        instances: list[InputInstance],
        *,
        raw_ingest_sources: set[str],
    ) -> tuple[InputConnectionState, InputCollectionState]:
        if source.activity_platforms:
            if not instances:
                if (
                    source.source_id == "activity.activitywatch"
                    and self._settings.activitywatch_enabled
                ):
                    return (
                        InputConnectionState.CONFIGURED,
                        InputCollectionState.IDLE,
                    )
                return (
                    InputConnectionState.NOT_CONFIGURED,
                    InputCollectionState.UNAVAILABLE,
                )
            reported = [
                instance
                for instance in instances
                if instance.status_observed_at is not None
            ]
            now = datetime.now(UTC)
            paused = any(
                instance.paused_until is not None
                and instance.paused_until > now
                for instance in instances
            )
            if not reported:
                return (
                    InputConnectionState.CONFIGURED,
                    InputCollectionState.PAUSED
                    if paused
                    else InputCollectionState.IDLE,
                )
            if any(
                instance.effective_collecting
                and instance.permission_status == "granted"
                and instance.capability in {"aggregate", "detailed"}
                for instance in reported
            ):
                collection = InputCollectionState.COLLECTING
            elif paused:
                collection = InputCollectionState.PAUSED
            elif all(not instance.enabled for instance in instances):
                collection = InputCollectionState.IDLE
            elif all(
                instance.capability == "unavailable"
                or instance.permission_status
                in {
                    "denied",
                    "restricted",
                    "revoked",
                    "unavailable",
                }
                for instance in reported
            ):
                collection = InputCollectionState.UNAVAILABLE
            elif any(instance.enabled for instance in instances):
                collection = InputCollectionState.BLOCKED
            else:
                collection = InputCollectionState.IDLE
            return InputConnectionState.CONNECTED, collection

        if source.source_id == "nutrition.capture":
            connection, default_collection = (
                InputConnectionState.CONFIGURED,
                InputCollectionState.IDLE,
            )
        elif source.source_id == "wearable.healthkit-bridge":
            connection, default_collection = (
                InputConnectionState.CONNECTED
                if "healthkit-bridge" in raw_ingest_sources
                else InputConnectionState.CONFIGURED,
                InputCollectionState.IDLE,
            )
        elif source.source_id == "wearable.open-wearables":
            configured = bool(
                self._settings.ow_api_key.get_secret_value().strip()
            )
            connection, default_collection = (
                InputConnectionState.CONFIGURED
                if configured
                else InputConnectionState.NOT_CONFIGURED,
                InputCollectionState.NOT_APPLICABLE,
            )
        else:
            if source.source_id == "calendar.google":
                connected = creds.google_connected(
                    self._settings.data_dir
                )
            elif source.source_id == "calendar.icloud":
                connected = (
                    creds.resolve_caldav_credentials(
                        self._settings
                    )
                    is not None
                )
            else:
                connected = False
            connection, default_collection = (
                InputConnectionState.CONNECTED
                if connected
                else InputConnectionState.NOT_CONFIGURED,
                InputCollectionState.NOT_APPLICABLE,
            )
        if not instances:
            return connection, default_collection
        now = datetime.now(UTC)
        if any(
            instance.paused_until is not None
            and instance.paused_until > now
            for instance in instances
        ):
            return connection, InputCollectionState.PAUSED
        return connection, InputCollectionState.IDLE
