"""REST contracts for the UI-independent Activity Wellness engine."""

from __future__ import annotations

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
from healthmes.activity.aggregation import rebuild_affected_days
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
    ActivityDeletionUnsafeError,
    delete_activity_data,
    run_activity_maintenance,
)
from healthmes.activity.repository import (
    ActivityConflictError,
    ActivityWriteConflictError,
    activity_write_lock,
    get_control_payload,
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
    session.commit()
    return ActivityCollectionOut.model_validate(serialize_collection_state(payload))


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


@router.get("/v1/activity/devices/{device_id}/collection")
def get_collection(
    device_id: CollectionDeviceId,
    session: SessionDep,
) -> ActivityCollectionOut:
    payload = get_control_payload(session, device_id)
    return ActivityCollectionOut.model_validate(serialize_collection_state(payload))


@router.put("/v1/activity/devices/{device_id}/collection")
def put_collection(
    device_id: CollectionDeviceId,
    body: ActivityCollectionUpdate,
    session: SessionDep,
) -> ActivityCollectionOut:
    with activity_write_lock():
        payload = update_collection_config(session, device_id, body)
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
        payload = update_collection_config(
            session,
            device_id,
            ActivityCollectionUpdate(paused_until=body.until),
        )
        return _commit_collection(session, payload)


@router.post("/v1/activity/devices/{device_id}/resume")
def resume_collection(
    device_id: CollectionDeviceId,
    session: SessionDep,
) -> ActivityCollectionOut:
    with activity_write_lock():
        payload = update_collection_config(
            session,
            device_id,
            ActivityCollectionUpdate(paused_until=None),
        )
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
    }
    boundary_touched = bool(boundary_fields & body.model_fields_set)
    generation_touched = "collection_generation" in body.model_fields_set
    if (body.platform is ActivityPlatform.ANDROID and boundary_touched) or (
        generation_touched
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
        available = (
            body.capability.value == "aggregate"
            and body.permission_status.value == "granted"
        )
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
                update_collection_status(
                    session,
                    body.device_id,
                    ActivityCollectionStatusUpdate(
                        platform=ActivityPlatform.IOS,
                        capability=body.capability,
                        permission_status=body.permission_status,
                        status_reason=body.reason,
                        status_observed_at=body.collected_at,
                        last_uploaded_at=uploaded_at,
                        last_collected_at=(
                            body.collected_at if available else None
                        ),
                    ),
                    now=uploaded_at,
                )
                if available and body.samples:
                    batch = ActivityBatchIn(
                        source_provider="ios-device-activity",
                        source_device=body.device_id,
                        platform=ActivityPlatform.IOS,
                        capability=body.capability,
                        timezone=body.timezone,
                        collected_at=body.collected_at,
                        collection_revision=body.collection_revision,
                        records=[
                            AppHourRecord(
                                source_record_id=scoped_source_record_id(
                                    prefix="ios-hour",
                                    device_id=body.device_id,
                                    source_record_id=sample.source_record_id,
                                ),
                                bucket_start=sample.bucket_start,
                                app_id=sample.opaque_app_token
                                or f"category:{sample.category}",
                                foreground_seconds=sample.foreground_seconds,
                                launches=sample.launches,
                                category=sample.category,
                                coverage_seconds=sample.coverage_seconds,
                                bucket_complete=True,
                            )
                            for sample in body.samples
                        ],
                    )
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
