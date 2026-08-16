"""REST contracts for the UI-independent Activity Wellness engine."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Path, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.activitywatch import (
    ActivityWatchError,
    ActivityWatchRequestError,
    StaleActivityWatchImportError,
    import_activitywatch,
    prepare_activitywatch_import,
)
from healthmes.activity.aggregation import (
    rebuild_affected_days,
    summary_raw_provenance_complete,
)
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityBatchOut,
    ActivityCapability,
    ActivityCollectionOut,
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityContextResolveRequest,
    ActivityDeleteRequest,
    ActivityMaintenanceOut,
    ActivityPauseRequest,
    ActivityPlatform,
    ActivityWatchImportRequest,
    AppHourRecord,
    AppIntervalRecord,
    IOSCapabilityReport,
    is_reserved_activity_provider,
)
from healthmes.activity.identity import scoped_source_record_id
from healthmes.activity.maintenance import (
    ActivityDeletionGranularityError,
    ActivityDeletionUnsafeError,
    delete_activity_data,
    run_activity_maintenance,
)
from healthmes.activity.privacy import collection_gate
from healthmes.activity.repository import (
    APP_HOUR_EVENT,
    IOS_PROVIDER,
    ActivityChangeWindow,
    ActivityConflictError,
    ActivityLocalScope,
    ActivityWriteConflictError,
    InvalidIOSAppTokenError,
    activity_write_lock,
    event_bounds,
    event_scopes,
    fixed_offset_summary_scopes_by_change,
    get_control_payload,
    get_ios_snapshot_fence,
    ios_exclusion_namespace,
    legacy_app_usage_cutoff,
    lock_activity_write_plane,
    parse_optional_datetime,
    persist_ios_snapshot_fence,
    range_scopes,
    serialize_collection_state,
    tombstoned_record_ids,
    update_collection_config,
    update_collection_status,
)
from healthmes.activity.resolver import (
    WellnessContextRangeError,
    resolve_wellness_context,
)
from healthmes.activity.service import (
    MAX_FUTURE_SKEW,
    ActivityCollectionBlockedError,
    ActivityFutureDataError,
    ActivityLateDataError,
    ActivitySourceModeConflictError,
    ActivitySummaryProvenanceError,
    StaleCollectionRevisionError,
    ingest_activity_batch,
    prepare_activity_batch,
)
from healthmes.api.errors import APIError
from healthmes.config import resolve_timezone
from healthmes.store import WellnessEvent
from healthmes.store.session import SessionDep
from healthmes.timezones import parse_timezone

router = APIRouter(tags=["activity"])
CollectionDeviceId = Annotated[str, Path(min_length=1, max_length=255)]


def _timezone(request: Request, explicit: str | None = None) -> str:
    if explicit is not None:
        try:
            parse_timezone(explicit)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise APIError(
                422,
                "invalid_timezone",
                "timezone is not a valid IANA name or UTC offset: "
                f"{explicit!r}",
            ) from exc
        return explicit
    try:
        return str(resolve_timezone(request.app.state.settings))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise APIError(
            422,
            "invalid_timezone",
            f"configured timezone is not a valid IANA name: {exc}",
        ) from exc


def _commit_collection(
    session: Session,
    payload: dict[str, Any],
) -> ActivityCollectionOut:
    serialized = serialize_collection_state(payload)
    serialized["raw_retention_cutoff"] = legacy_app_usage_cutoff(session)
    session.commit()
    return ActivityCollectionOut.model_validate(serialized)


def _scope_public_batch_source_ids(
    session: Session,
    body: ActivityBatchIn,
) -> ActivityBatchIn:
    legacy_tombstoned_ids = tombstoned_record_ids(
        session,
        source_provider=body.source_provider,
        device_id=body.source_device,
        records=body.records,
    )
    source_record_ids = {record.source_record_id for record in body.records}
    legacy_rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.source_provider == body.source_provider,
                WellnessEvent.source_record_id.in_(source_record_ids),
                WellnessEvent.source_device == body.source_device,
            )
        )
    )
    legacy_record_ids = {
        row.source_record_id for row in legacy_rows
    } | legacy_tombstoned_ids
    legacy_group_ids = {
        str(group_id)
        for row in legacy_rows
        if isinstance(row.payload, dict)
        and (group_id := row.payload.get("source_group_id")) is not None
    }
    records = []
    for record in body.records:
        updates = {
            "source_record_id": (
                record.source_record_id
                if record.source_record_id in legacy_record_ids
                else scoped_source_record_id(
                    prefix="generic-record",
                    device_id=body.source_device,
                    source_record_id=record.source_record_id,
                )
            )
        }
        if (
            isinstance(record, AppIntervalRecord)
            and record.source_group_id is not None
        ):
            updates["source_group_id"] = (
                record.source_group_id
                if record.source_group_id in legacy_group_ids
                else scoped_source_record_id(
                    prefix="generic-group",
                    device_id=body.source_device,
                    source_record_id=record.source_group_id,
                )
            )
        records.append(record.model_copy(update=updates))
    return body.model_copy(update={"records": records})


def _ios_snapshot_manifest_digest(body: IOSCapabilityReport) -> str:
    assert body.snapshot_start is not None
    assert body.snapshot_end is not None
    payload = {
        "snapshot_start": body.snapshot_start.isoformat(),
        "snapshot_end": body.snapshot_end.isoformat(),
        "timezone": body.timezone,
        "capability": body.capability.value,
        "permission_status": body.permission_status.value,
        "pseudonym_key_id": body.pseudonym_key_id,
        "collection_revision": body.collection_revision,
        "collection_generation": body.collection_generation,
        "authoritative_bucket_starts": [
            value.isoformat()
            for value in body.authoritative_bucket_starts
        ],
        "samples": [
            sample.model_dump(mode="json")
            for sample in sorted(
                body.samples,
                key=lambda item: item.source_record_id,
            )
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def _ios_batch(body: IOSCapabilityReport) -> ActivityBatchIn:
    def source_identity(source_record_id: str) -> str:
        if body.collection_generation is None:
            return source_record_id
        return (
            f"generation:{body.collection_generation}:"
            f"record:{source_record_id}"
        )

    records: list[AppHourRecord] = []
    for sample in body.samples:
        if sample.coverage_only:
            app_id = "__healthmes_coverage__"
        else:
            if sample.opaque_app_token is None:
                raise AssertionError(
                    "validated iOS activity sample is missing its app token"
                )
            app_id = sample.opaque_app_token
        records.append(
            AppHourRecord(
                source_record_id=scoped_source_record_id(
                    prefix="ios-hour",
                    device_id=body.device_id,
                    source_record_id=source_identity(
                        sample.source_record_id
                    ),
                ),
                bucket_start=sample.bucket_start,
                app_id=app_id,
                foreground_seconds=sample.foreground_seconds,
                launches=0,
                launches_observed=False,
                category=(
                    None if sample.coverage_only else sample.category
                ),
                coverage_seconds=sample.coverage_seconds,
                coverage_only=sample.coverage_only,
                bucket_complete=True,
                snapshot_sequence=body.snapshot_sequence,
            )
        )

    return ActivityBatchIn(
        source_provider=IOS_PROVIDER,
        source_device=body.device_id,
        platform=ActivityPlatform.IOS,
        capability=body.capability,
        timezone=body.timezone,
        collected_at=body.collected_at,
        collection_revision=body.collection_revision,
        records=records,
    )


def _validate_ios_empty_snapshot_gate(
    state: dict[str, Any],
    body: IOSCapabilityReport,
    *,
    now: datetime,
) -> None:
    revision = int(state.get("config_revision", 0))
    if body.collection_revision != revision:
        raise StaleCollectionRevisionError(
            f"collector configuration revision {body.collection_revision} "
            f"does not match server revision {revision}"
        )
    gate = collection_gate(state, now=now)
    if not gate.allowed:
        raise ActivityCollectionBlockedError(
            gate.reason or "collection_blocked"
        )


def _ios_authoritative_scopes(
    session: Session,
    *,
    body: IOSCapabilityReport,
    records: list[AppHourRecord],
    existing_rows: list[WellnessEvent],
    now: datetime,
) -> set[ActivityLocalScope]:
    assert body.snapshot_start is not None
    assert body.snapshot_end is not None
    scopes = {
        scope
        for bucket_start in body.authoritative_bucket_starts
        for scope in range_scopes(
            start=bucket_start,
            end=bucket_start + timedelta(hours=1),
            timezone=body.timezone,
        )
    }
    scopes.update(
        scope
        for record in records
        for scope in range_scopes(
            start=record.bucket_start,
            end=record.bucket_start + timedelta(hours=1),
            timezone=body.timezone,
        )
    )
    scopes.update(
        scope
        for row in existing_rows
        for scope in event_scopes(row)
    )
    changes = [
        ActivityChangeWindow(
            key=f"authoritative-bucket:{bucket_start.isoformat()}",
            start=bucket_start,
            end=bucket_start + timedelta(hours=1),
            timezone=body.timezone,
        )
        for bucket_start in body.authoritative_bucket_starts
    ]
    changes.extend(
        ActivityChangeWindow(
            key=f"incoming:{record.source_record_id}",
            start=record.bucket_start,
            end=record.bucket_start + timedelta(hours=1),
            timezone=body.timezone,
        )
        for record in records
    )
    for row in existing_rows:
        start, end = event_bounds(row)
        changes.append(
            ActivityChangeWindow(
                key=f"existing:{row.id}",
                start=start,
                end=end,
                timezone=row.timezone or "UTC",
            )
        )
    scopes.update(
        scope
        for values in fixed_offset_summary_scopes_by_change(
            session,
            changes,
            now=now,
        ).values()
        for scope in values
    )
    incomplete = [
        scope
        for scope in sorted(scopes)
        if not summary_raw_provenance_complete(
            session,
            day=scope.day,
            timezone=scope.timezone,
            now=now,
        )
    ]
    if incomplete:
        raise ActivitySummaryProvenanceError(
            "iOS snapshot replacement requires retained raw provenance "
            f"for {len(incomplete)} summary scope(s)"
        )
    return scopes


@router.get("/v1/activity/devices/{device_id}/collection")
def get_collection(
    device_id: CollectionDeviceId,
    session: SessionDep,
    platform: ActivityPlatform | None = None,
) -> ActivityCollectionOut:
    payload = get_control_payload(
        session,
        device_id,
        platform=platform or ActivityPlatform.UNKNOWN,
    )
    serialized = serialize_collection_state(payload)
    serialized["raw_retention_cutoff"] = legacy_app_usage_cutoff(session)
    return ActivityCollectionOut.model_validate(serialized)


@router.put("/v1/activity/devices/{device_id}/collection")
def put_collection(
    device_id: CollectionDeviceId,
    body: ActivityCollectionUpdate,
    session: SessionDep,
) -> ActivityCollectionOut:
    with activity_write_lock():
        try:
            payload = update_collection_config(session, device_id, body)
        except InvalidIOSAppTokenError as exc:
            raise APIError(
                422,
                "invalid_ios_app_token",
                str(exc),
            ) from exc
        return _commit_collection(session, payload)


@router.post("/v1/activity/devices/{device_id}/pause")
def pause_collection(
    device_id: CollectionDeviceId,
    body: ActivityPauseRequest,
    session: SessionDep,
) -> ActivityCollectionOut:
    if body.until <= datetime.now(UTC):
        raise APIError(422, "invalid_pause", "pause deadline must be in the future")
    with activity_write_lock():
        try:
            payload = update_collection_config(
                session,
                device_id,
                ActivityCollectionUpdate(paused_until=body.until),
            )
        except InvalidIOSAppTokenError as exc:
            raise APIError(
                422,
                "invalid_ios_app_token",
                str(exc),
            ) from exc
        return _commit_collection(session, payload)


@router.post("/v1/activity/devices/{device_id}/resume")
def resume_collection(
    device_id: CollectionDeviceId,
    session: SessionDep,
) -> ActivityCollectionOut:
    with activity_write_lock():
        try:
            payload = update_collection_config(
                session,
                device_id,
                ActivityCollectionUpdate(paused_until=None),
            )
        except InvalidIOSAppTokenError as exc:
            raise APIError(
                422,
                "invalid_ios_app_token",
                str(exc),
            ) from exc
        return _commit_collection(session, payload)


@router.post("/v1/activity/devices/{device_id}/status")
def post_collection_status(
    device_id: CollectionDeviceId,
    body: ActivityCollectionStatusUpdate,
    session: SessionDep,
) -> ActivityCollectionOut:
    boundary_fields = {
        "permission_status",
        "status_observed_at",
        "collection_generation",
        "pairing_revision",
    }
    boundary_touched = bool(boundary_fields & body.model_fields_set)
    monotonic_boundary_touched = bool(
        {"collection_generation", "pairing_revision"} & body.model_fields_set
    )
    if (body.platform is ActivityPlatform.ANDROID and boundary_touched) or (
        monotonic_boundary_touched
    ):
        complete_android_boundary = (
            body.platform is ActivityPlatform.ANDROID
            and body.capability is ActivityCapability.AGGREGATE
            and body.permission_status is not None
            and body.status_observed_at is not None
            and body.collection_generation is not None
        )
        if not complete_android_boundary:
            raise APIError(
                422,
                "activity_status_boundary_required",
                "Android status boundary requires platform=android, "
                "capability=aggregate, permission_status, status_observed_at, "
                "and collection_generation",
            )
    observed_at = body.status_observed_at
    current = datetime.now(UTC)
    if observed_at is not None and observed_at > current + MAX_FUTURE_SKEW:
        raise APIError(
            409,
            "activity_future_data",
            "status_observed_at is beyond the allowed one-minute clock skew",
        )
    with activity_write_lock():
        payload = update_collection_status(
            session,
            device_id,
            body,
            now=current,
        )
        return _commit_collection(session, payload)


@router.post("/v1/activity/events/batch")
def post_activity_batch(
    body: ActivityBatchIn,
    session: SessionDep,
) -> ActivityBatchOut:
    if body.collection_revision is None:
        raise APIError(
            422,
            "activity_collection_revision_required",
            "public activity ingest requires collection_revision",
        )
    if is_reserved_activity_provider(body.source_provider):
        raise APIError(
            422,
            "activity_provider_reserved",
            "built-in activity providers are available only through their adapters",
        )
    with activity_write_lock():
        body = _scope_public_batch_source_ids(session, body)
        try:
            result = ingest_activity_batch(session, body)
        except ActivityCollectionBlockedError as exc:
            raise APIError(409, "activity_collection_blocked", exc.reason) from exc
        except StaleCollectionRevisionError as exc:
            raise APIError(409, "stale_collection_revision", str(exc)) from exc
        except ActivityConflictError as exc:
            raise APIError(409, "activity_source_conflict", str(exc)) from exc
        except ActivityLateDataError as exc:
            raise APIError(409, "activity_outside_retention", str(exc)) from exc
        except ActivityFutureDataError as exc:
            raise APIError(409, "activity_future_data", str(exc)) from exc
        except ActivitySourceModeConflictError as exc:
            raise APIError(409, "activity_source_mode_conflict", str(exc)) from exc
        except ActivitySummaryProvenanceError as exc:
            raise APIError(
                409,
                "activity_summary_requires_complete_raw",
                str(exc),
            ) from exc
        except ActivityWriteConflictError as exc:
            raise APIError(409, "activity_write_conflict", str(exc)) from exc
        session.commit()
        return result.response


@router.post("/v1/activity/activitywatch/import")
def post_activitywatch_import(
    body: ActivityWatchImportRequest,
    session: SessionDep,
) -> ActivityBatchOut:
    try:
        prepared = prepare_activitywatch_import(session, body)
        with activity_write_lock():
            result = import_activitywatch(
                session,
                body,
                prepared=prepared,
            )
            session.commit()
    except ActivityCollectionBlockedError as exc:
        raise APIError(409, "activity_collection_blocked", exc.reason) from exc
    except StaleCollectionRevisionError as exc:
        raise APIError(409, "stale_collection_revision", str(exc)) from exc
    except StaleActivityWatchImportError as exc:
        raise APIError(409, "stale_activitywatch_import", str(exc)) from exc
    except ActivityLateDataError as exc:
        raise APIError(409, "activity_outside_retention", str(exc)) from exc
    except ActivityFutureDataError as exc:
        raise APIError(409, "activity_future_data", str(exc)) from exc
    except ActivitySourceModeConflictError as exc:
        raise APIError(409, "activity_source_mode_conflict", str(exc)) from exc
    except ActivitySummaryProvenanceError as exc:
        raise APIError(
            409,
            "activity_summary_requires_complete_raw",
            str(exc),
        ) from exc
    except ActivityWatchRequestError as exc:
        raise APIError(
            422,
            "invalid_activitywatch_range",
            str(exc),
        ) from exc
    except (ActivityWatchError, httpx.HTTPError) as exc:
        raise APIError(
            502,
            "activitywatch_error",
            f"ActivityWatch import failed: {exc}",
        ) from exc
    return ActivityBatchOut.model_validate(result.response)


@router.post("/v1/activity/ios/report")
def post_ios_report(
    body: IOSCapabilityReport,
    session: SessionDep,
) -> ActivityBatchOut:
    with activity_write_lock():
        uploaded_at = datetime.now(UTC)
        if body.collected_at > uploaded_at + MAX_FUTURE_SKEW:
            raise APIError(
                409,
                "activity_future_data",
                "iOS collected_at is beyond the allowed one-minute clock skew",
            )
        if (
            body.snapshot_end is not None
            and body.snapshot_end > uploaded_at + MAX_FUTURE_SKEW
        ):
            raise APIError(
                409,
                "activity_future_data",
                "iOS snapshot range extends beyond the allowed one-minute "
                "clock skew",
            )
        available = (
            body.capability.value == "aggregate"
            and body.permission_status.value == "granted"
        )
        authoritative = body.snapshot_sequence is not None
        response = ActivityBatchOut(
            accepted=0,
            created=0,
            updated=0,
            duplicates=0,
            excluded=0,
            affected_dates=[],
        )
        try:
            with session.begin_nested():
                lock_activity_write_plane(session)
                snapshot_fence = get_ios_snapshot_fence(
                    session,
                    body.device_id,
                    lock=True,
                )
                manifest_sha256: str | None = None
                if available and authoritative:
                    assert body.snapshot_sequence is not None
                    assert body.snapshot_start is not None
                    assert body.snapshot_end is not None
                    manifest_sha256 = _ios_snapshot_manifest_digest(body)
                    if (
                        snapshot_fence is not None
                        and body.collection_generation
                        == snapshot_fence.collection_generation
                        and body.snapshot_sequence
                        == snapshot_fence.sequence
                    ):
                        if (
                            manifest_sha256
                            != snapshot_fence.manifest_sha256
                        ):
                            raise ActivityConflictError(
                                "iOS snapshot sequence was reused with "
                                "different content"
                            )
                        if snapshot_fence.accepted_response is not None:
                            return snapshot_fence.accepted_response
                        raise APIError(
                            409,
                            "snapshot_retry_response_unavailable",
                            "This legacy iOS snapshot was accepted, but its "
                            "exact response is unavailable; advance the "
                            "snapshot sequence before retrying",
                        )
                previous_state = get_control_payload(
                    session,
                    body.device_id,
                    platform=ActivityPlatform.IOS,
                    lock=True,
                )
                configured_exclusions = list(
                    previous_state.get("excluded_apps") or []
                )
                configured_pseudonym_key_id = previous_state.get(
                    "ios_pseudonym_key_id"
                )
                exclusions_valid, exclusion_key_id = (
                    ios_exclusion_namespace(configured_exclusions)
                )
                if (
                    available
                    and configured_exclusions
                    and (
                        not exclusions_valid
                        or exclusion_key_id is None
                        or configured_pseudonym_key_id
                        != exclusion_key_id
                    )
                ):
                    raise APIError(
                        409,
                        "ios_exclusion_reapproval_required",
                        "Stored iOS exclusions must be cleared or "
                        "re-approved with the current device pseudonym key",
                    )
                if (
                    available
                    and configured_pseudonym_key_id is not None
                    and body.pseudonym_key_id
                    != configured_pseudonym_key_id
                ):
                    raise APIError(
                        409,
                        "ios_exclusion_reapproval_required",
                        "iOS report pseudonym key does not match the "
                        "configured exclusion namespace",
                    )
                previous_collected_at = parse_optional_datetime(
                    previous_state.get("last_collected_at")
                )
                if (
                    not authoritative
                    and available
                    and body.samples
                    and snapshot_fence is not None
                ):
                    raise ActivityConflictError(
                        "iOS device already uses ordered authoritative "
                        "snapshots; sequence-less samples cannot be mixed in"
                    )
                if (
                    not authoritative
                    and available
                    and body.samples
                    and previous_collected_at is not None
                    and body.collected_at < previous_collected_at
                ):
                    raise ActivityConflictError(
                        "iOS aggregate report is older than the latest "
                        "accepted device snapshot"
                    )
                apply_snapshot = True
                if available and authoritative:
                    assert body.snapshot_sequence is not None
                    assert body.snapshot_start is not None
                    assert body.snapshot_end is not None
                    assert manifest_sha256 is not None
                    if snapshot_fence is not None:
                        generation_changed = (
                            body.collection_generation
                            != snapshot_fence.collection_generation
                        )
                        if generation_changed:
                            if not body.reset_snapshot_fence:
                                raise APIError(
                                    409,
                                    "activity_snapshot_fence_reset_required",
                                    "iOS snapshot collection generation changed "
                                    "without an authenticated fence reset",
                                )
                            if (
                                snapshot_fence.collection_generation is not None
                                and (
                                    body.collection_generation is None
                                    or body.collection_generation
                                    <= snapshot_fence.collection_generation
                                )
                            ):
                                raise ActivityConflictError(
                                    "iOS snapshot fence reset requires a newer "
                                    "collection generation"
                                )
                        else:
                            if body.reset_snapshot_fence:
                                raise ActivityConflictError(
                                    "iOS snapshot fence reset requires a new "
                                    "collection generation"
                                )
                            if body.snapshot_sequence < snapshot_fence.sequence:
                                raise ActivityConflictError(
                                    "iOS snapshot is older than the latest "
                                    "accepted sequence"
                                )
                            if body.snapshot_sequence == snapshot_fence.sequence:
                                if (
                                    manifest_sha256
                                    != snapshot_fence.manifest_sha256
                                ):
                                    raise ActivityConflictError(
                                        "iOS snapshot sequence was reused with "
                                        "different content"
                                    )
                                apply_snapshot = False
                    elif body.reset_snapshot_fence:
                        raise ActivityConflictError(
                            "iOS snapshot fence reset requires an existing fence"
                        )

                status_values: dict[str, Any] = {
                    "platform": ActivityPlatform.IOS,
                    "last_uploaded_at": uploaded_at,
                }
                if apply_snapshot or not (available and authoritative):
                    status_values.update(
                        {
                            "capability": body.capability,
                            "permission_status": body.permission_status,
                            "status_reason": body.reason,
                            "status_observed_at": body.collected_at,
                            "last_collected_at": (
                                body.collected_at if available else None
                            ),
                        }
                    )
                    if body.collection_generation is not None:
                        status_values["collection_generation"] = (
                            body.collection_generation
                        )
                update_collection_status(
                    session,
                    body.device_id,
                    ActivityCollectionStatusUpdate.model_validate(
                        status_values
                    ),
                    now=uploaded_at,
                )
                if available and authoritative:
                    assert body.snapshot_sequence is not None
                    assert body.snapshot_start is not None
                    assert body.snapshot_end is not None
                    assert manifest_sha256 is not None

                    latest_state = get_control_payload(
                        session,
                        body.device_id,
                        platform=ActivityPlatform.IOS,
                        lock=True,
                    )
                    if body.samples:
                        prepared, excluded, tombstoned, _ = (
                            prepare_activity_batch(
                                session,
                                _ios_batch(body),
                                now=uploaded_at,
                                control_payload=latest_state,
                            )
                        )
                    else:
                        _validate_ios_empty_snapshot_gate(
                            latest_state,
                            body,
                            now=uploaded_at,
                        )
                        prepared = None
                        excluded = 0
                        tombstoned = 0

                    if not apply_snapshot:
                        accepted = (
                            len(prepared.records)
                            if prepared is not None
                            else 0
                        )
                        response = ActivityBatchOut(
                            accepted=accepted,
                            created=0,
                            updated=0,
                            duplicates=accepted,
                            excluded=excluded,
                            tombstoned=tombstoned,
                            affected_dates=[],
                        )
                    else:
                        allowed_records = (
                            [
                                record
                                for record in prepared.records
                                if isinstance(record, AppHourRecord)
                            ]
                            if prepared is not None
                            else []
                        )
                        expected_source_ids = {
                            record.source_record_id
                            for record in allowed_records
                        }
                        # Authoritative means complete after the device's
                        # privacy filter, not complete raw Screen Time
                        # coverage. Query the whole replacement scope so an
                        # app newly made private is deleted even when allowed
                        # samples remain in the same hour.
                        existing_rows = (
                            list(
                                session.scalars(
                                    select(WellnessEvent)
                                    .where(
                                        WellnessEvent.event_type
                                        == APP_HOUR_EVENT,
                                        WellnessEvent.source_provider
                                        == IOS_PROVIDER,
                                        WellnessEvent.source_device
                                        == body.device_id,
                                        WellnessEvent.observed_at.in_(
                                            body.authoritative_bucket_starts
                                        ),
                                    )
                                    .with_for_update()
                                    .execution_options(
                                        populate_existing=True
                                    )
                                )
                            )
                            if body.authoritative_bucket_starts
                            else []
                        )
                        scopes = _ios_authoritative_scopes(
                            session,
                            body=body,
                            records=allowed_records,
                            existing_rows=existing_rows,
                            now=uploaded_at,
                        )
                        for row in existing_rows:
                            if row.source_record_id not in expected_source_ids:
                                session.delete(row)
                        session.flush()

                        if prepared is not None and allowed_records:
                            prepared = prepared.model_copy(
                                update={"records": allowed_records}
                            )
                            result = ingest_activity_batch(
                                session,
                                prepared,
                                allow_replace=True,
                                now=uploaded_at,
                                already_filtered=True,
                                excluded_count=excluded,
                                tombstoned_count=tombstoned,
                                rebuild_summaries=False,
                                prevalidated_summary_scopes=scopes,
                            )
                            response = result.response.model_copy(
                                update={
                                    "affected_dates": [
                                        value.isoformat()
                                        for value in sorted(
                                            {
                                                scope.day
                                                for scope in scopes
                                            }
                                        )
                                    ]
                                }
                            )
                            scopes.update(result.changed_scopes)
                        else:
                            response = ActivityBatchOut(
                                accepted=0,
                                created=0,
                                updated=0,
                                duplicates=0,
                                excluded=excluded,
                                tombstoned=tombstoned,
                                affected_dates=[
                                    value.isoformat()
                                    for value in sorted(
                                        {scope.day for scope in scopes}
                                    )
                                ],
                            )

                        by_timezone: dict[str, set[date]] = {}
                        for scope in scopes:
                            by_timezone.setdefault(
                                scope.timezone,
                                set(),
                            ).add(scope.day)
                        for timezone, days in by_timezone.items():
                            rebuild_affected_days(
                                session,
                                days=days,
                                timezone=timezone,
                                force_rebuild=True,
                                now=uploaded_at,
                            )
                        persist_ios_snapshot_fence(
                            session,
                            body.device_id,
                            collection_generation=body.collection_generation,
                            sequence=body.snapshot_sequence,
                            manifest_sha256=manifest_sha256,
                            snapshot_start=body.snapshot_start,
                            snapshot_end=body.snapshot_end,
                            accepted_response=response,
                            now=uploaded_at,
                        )
                elif available and body.samples:
                    batch = _ios_batch(body)
                    response = ingest_activity_batch(
                        session,
                        batch,
                        allow_replace=True,
                    ).response
        except ActivityCollectionBlockedError as exc:
            raise APIError(409, "activity_collection_blocked", exc.reason) from exc
        except StaleCollectionRevisionError as exc:
            raise APIError(409, "stale_collection_revision", str(exc)) from exc
        except ActivityLateDataError as exc:
            raise APIError(409, "activity_outside_retention", str(exc)) from exc
        except ActivityFutureDataError as exc:
            raise APIError(409, "activity_future_data", str(exc)) from exc
        except ActivitySourceModeConflictError as exc:
            raise APIError(409, "activity_source_mode_conflict", str(exc)) from exc
        except ActivityConflictError as exc:
            raise APIError(409, "activity_source_conflict", str(exc)) from exc
        except ActivityWriteConflictError as exc:
            raise APIError(409, "activity_write_conflict", str(exc)) from exc
        except ActivitySummaryProvenanceError as exc:
            raise APIError(
                409,
                "activity_summary_requires_complete_raw",
                str(exc),
            ) from exc
        session.commit()
        return response


@router.get("/v1/activity/summary")
def get_activity_summary(
    request: Request,
    session: SessionDep,
    local_date: Annotated[date | None, Query(alias="date")] = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    zone = _timezone(request, timezone)
    day = local_date or datetime.now(parse_timezone(zone)).date()
    return activity_summary_context(session, day=day, timezone=zone)


@router.get("/v1/activity/focus-context")
def get_focus_context(
    request: Request,
    session: SessionDep,
    start: datetime,
    end: datetime,
    timezone: str | None = None,
) -> dict[str, Any]:
    if start.tzinfo is None or end.tzinfo is None:
        raise APIError(422, "invalid_time_range", "start and end must include timezone offsets")
    if start >= end:
        raise APIError(422, "invalid_time_range", "start must be before end")
    if end - start > timedelta(days=1):
        raise APIError(422, "invalid_time_range", "focus window cannot exceed 24 hours")
    if end.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=1):
        raise APIError(422, "invalid_time_range", "future activity is unknown")
    return focus_context(
        session,
        start=start.astimezone(UTC),
        end=end.astimezone(UTC),
        timezone=_timezone(request, timezone),
    )


@router.get("/v1/activity/overwork-context")
def get_overwork_context(
    request: Request,
    session: SessionDep,
    local_date: Annotated[date | None, Query(alias="date")] = None,
    lookback_days: Annotated[int, Query(ge=1, le=90)] = 7,
    timezone: str | None = None,
) -> dict[str, Any]:
    zone = _timezone(request, timezone)
    day = local_date or datetime.now(parse_timezone(zone)).date()
    return overwork_context(
        session,
        day=day,
        timezone=zone,
        lookback_days=lookback_days,
    )


@router.post("/v1/wellness-context/resolve")
async def post_wellness_context(
    request: Request,
    body: ActivityContextResolveRequest,
    session: SessionDep,
) -> dict[str, Any]:
    zone = _timezone(request, body.timezone)

    async def wearable_reader(day: date) -> dict[str, Any]:
        from healthmes.mcp_server.server import get_daily_readiness_context

        return await get_daily_readiness_context(day.isoformat())

    try:
        return await resolve_wellness_context(
            session,
            body,
            default_timezone=zone,
            wearable_reader=wearable_reader,
            calendar_settings=request.app.state.settings,
        )
    except WellnessContextRangeError as exc:
        raise APIError(422, "invalid_context_range", str(exc)) from exc


@router.post("/v1/activity/data/delete")
def post_delete_activity(
    request: Request,
    body: ActivityDeleteRequest,
    session: SessionDep,
) -> dict[str, Any]:
    with activity_write_lock():
        try:
            report = delete_activity_data(
                session,
                device_id=body.device_id,
                start=body.start,
                end=body.end,
                include_summaries=body.include_summaries,
                include_control=body.include_control,
            )
        except ActivityDeletionUnsafeError as exc:
            raise APIError(
                409,
                "activity_deletion_requires_complete_raw",
                str(exc),
            ) from exc
        except ActivityDeletionGranularityError as exc:
            raise APIError(
                422,
                "activity_deletion_granularity",
                str(exc),
            ) from exc
        except ValueError as exc:
            raise APIError(422, "invalid_delete_range", str(exc)) from exc
        by_timezone: dict[str, set[date]] = {}
        for scope in report.affected_scopes:
            by_timezone.setdefault(scope.timezone, set()).add(scope.day)
        for zone, days in by_timezone.items():
            rebuild_affected_days(
                session,
                days=days,
                timezone=zone,
                force_rebuild=True,
            )
        session.commit()
        return {
            "raw_events_deleted": report.raw_events_deleted,
            "summary_events_deleted": report.summary_events_deleted,
            "control_events_deleted": report.control_events_deleted,
            "compatibility_rows_deleted": report.compatibility_rows_deleted,
            "affected_dates": [
                value.isoformat() for value in report.affected_dates
            ],
            "tombstone_id": report.tombstone_id,
        }


@router.post("/v1/activity/maintenance")
def post_activity_maintenance(
    session: SessionDep,
) -> ActivityMaintenanceOut:
    with activity_write_lock():
        report = run_activity_maintenance(session)
        session.commit()
        return ActivityMaintenanceOut(
            expired_events_deleted=report.expired_events_deleted,
            compatibility_rows_deleted=report.compatibility_rows_deleted,
            affected_dates=[value.isoformat() for value in report.affected_dates],
        )
