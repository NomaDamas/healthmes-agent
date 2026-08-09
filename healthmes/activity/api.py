"""REST contracts for the UI-independent Activity Wellness engine."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Query, Request
from sqlalchemy.orm import Session

from healthmes.activity.activitywatch import (
    ActivityWatchError,
    import_activitywatch,
)
from healthmes.activity.aggregation import rebuild_affected_days, rebuild_day_summaries
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityBatchOut,
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
    IOSCapabilityReport,
)
from healthmes.activity.identity import scoped_source_record_id
from healthmes.activity.maintenance import (
    delete_activity_data,
    run_activity_maintenance,
)
from healthmes.activity.repository import (
    ActivityConflictError,
    get_control_payload,
    serialize_collection_state,
    update_collection_config,
    update_collection_status,
)
from healthmes.activity.resolver import resolve_wellness_context
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    StaleCollectionRevisionError,
    ingest_activity_batch,
)
from healthmes.api.errors import APIError
from healthmes.config import resolve_timezone
from healthmes.store.session import SessionDep

router = APIRouter(tags=["activity"])


def _timezone(request: Request, explicit: str | None = None) -> str:
    if explicit is not None:
        try:
            ZoneInfo(explicit)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise APIError(
                422,
                "invalid_timezone",
                f"timezone is not a valid IANA name: {explicit!r}",
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


@router.get("/v1/activity/devices/{device_id}/collection")
def get_collection(
    device_id: str,
    session: SessionDep,
) -> ActivityCollectionOut:
    payload = get_control_payload(session, device_id)
    return ActivityCollectionOut.model_validate(serialize_collection_state(payload))


@router.put("/v1/activity/devices/{device_id}/collection")
def put_collection(
    device_id: str,
    body: ActivityCollectionUpdate,
    session: SessionDep,
) -> ActivityCollectionOut:
    payload = update_collection_config(session, device_id, body)
    return _commit_collection(session, payload)


@router.post("/v1/activity/devices/{device_id}/pause")
def pause_collection(
    device_id: str,
    body: ActivityPauseRequest,
    session: SessionDep,
) -> ActivityCollectionOut:
    if body.until <= datetime.now(UTC):
        raise APIError(422, "invalid_pause", "pause deadline must be in the future")
    payload = update_collection_config(
        session,
        device_id,
        ActivityCollectionUpdate(paused_until=body.until),
    )
    return _commit_collection(session, payload)


@router.post("/v1/activity/devices/{device_id}/resume")
def resume_collection(
    device_id: str,
    session: SessionDep,
) -> ActivityCollectionOut:
    payload = update_collection_config(
        session,
        device_id,
        ActivityCollectionUpdate(paused_until=None),
    )
    return _commit_collection(session, payload)


@router.post("/v1/activity/devices/{device_id}/status")
def post_collection_status(
    device_id: str,
    body: ActivityCollectionStatusUpdate,
    session: SessionDep,
) -> ActivityCollectionOut:
    payload = update_collection_status(session, device_id, body)
    return _commit_collection(session, payload)


@router.post("/v1/activity/events/batch")
def post_activity_batch(
    body: ActivityBatchIn,
    session: SessionDep,
) -> ActivityBatchOut:
    try:
        result = ingest_activity_batch(session, body)
    except ActivityCollectionBlockedError as exc:
        raise APIError(409, "activity_collection_blocked", exc.reason) from exc
    except StaleCollectionRevisionError as exc:
        raise APIError(409, "stale_collection_revision", str(exc)) from exc
    except ActivityConflictError as exc:
        raise APIError(409, "activity_source_conflict", str(exc)) from exc
    session.commit()
    return result.response


@router.post("/v1/activity/activitywatch/import")
def post_activitywatch_import(
    body: ActivityWatchImportRequest,
    session: SessionDep,
) -> ActivityBatchOut:
    try:
        result = import_activitywatch(session, body)
    except ActivityCollectionBlockedError as exc:
        raise APIError(409, "activity_collection_blocked", exc.reason) from exc
    except StaleCollectionRevisionError as exc:
        raise APIError(409, "stale_collection_revision", str(exc)) from exc
    except (ActivityWatchError, httpx.HTTPError) as exc:
        raise APIError(
            502,
            "activitywatch_error",
            f"ActivityWatch import failed: {exc}",
        ) from exc
    session.commit()
    return ActivityBatchOut.model_validate(result.response)


@router.post("/v1/activity/ios/report")
def post_ios_report(
    body: IOSCapabilityReport,
    session: SessionDep,
) -> ActivityBatchOut:
    uploaded_at = datetime.now(UTC)
    status = update_collection_status(
        session,
        body.device_id,
        ActivityCollectionStatusUpdate(
            platform=ActivityPlatform.IOS,
            capability=body.capability,
            permission_status=body.permission_status,
            status_reason=body.reason,
            last_uploaded_at=uploaded_at,
            last_collected_at=(
                body.collected_at
                if body.capability.value == "aggregate"
                and body.permission_status.value == "granted"
                else None
            ),
        ),
        now=uploaded_at,
    )
    available = body.capability.value == "aggregate" and body.permission_status.value == "granted"
    if not available or not body.samples:
        session.commit()
        return ActivityBatchOut(
            accepted=0,
            created=0,
            updated=0,
            duplicates=0,
            excluded=0,
            affected_dates=[],
        )
    batch = ActivityBatchIn(
        source_provider="ios-device-activity",
        source_device=body.device_id,
        platform=ActivityPlatform.IOS,
        capability=body.capability,
        timezone=body.timezone,
        collected_at=body.collected_at,
        collection_revision=int(status.get("config_revision", 0)),
        records=[
            AppHourRecord(
                source_record_id=scoped_source_record_id(
                    prefix="ios-hour",
                    device_id=body.device_id,
                    source_record_id=sample.source_record_id,
                ),
                bucket_start=sample.bucket_start,
                app_id=sample.opaque_app_token or f"category:{sample.category}",
                foreground_seconds=sample.foreground_seconds,
                launches=sample.launches,
                category=sample.category,
                coverage_seconds=sample.coverage_seconds,
                bucket_complete=True,
            )
            for sample in body.samples
        ],
    )
    try:
        result = ingest_activity_batch(session, batch, allow_replace=True)
    except ActivityCollectionBlockedError as exc:
        raise APIError(409, "activity_collection_blocked", exc.reason) from exc
    session.commit()
    return result.response


@router.get("/v1/activity/summary")
def get_activity_summary(
    request: Request,
    session: SessionDep,
    local_date: Annotated[date | None, Query(alias="date")] = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    zone = _timezone(request, timezone)
    day = local_date or datetime.now(ZoneInfo(zone)).date()
    context = activity_summary_context(session, day=day, timezone=zone)
    if context["status"] == "insufficient_data":
        rebuilt = rebuild_day_summaries(session, day=day, timezone=zone)
        if rebuilt is not None:
            session.commit()
            context = activity_summary_context(session, day=day, timezone=zone)
    return context


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
    day = local_date or datetime.now(ZoneInfo(zone)).date()
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

    return await resolve_wellness_context(
        session,
        body,
        default_timezone=zone,
        wearable_reader=wearable_reader,
    )


@router.post("/v1/activity/data/delete")
def post_delete_activity(
    request: Request,
    body: ActivityDeleteRequest,
    session: SessionDep,
) -> dict[str, Any]:
    report = delete_activity_data(
        session,
        device_id=body.device_id,
        start=body.start,
        end=body.end,
        include_summaries=body.include_summaries,
        include_control=body.include_control,
    )
    by_timezone: dict[str, set[date]] = {}
    for scope in report.affected_scopes:
        by_timezone.setdefault(scope.timezone, set()).add(scope.day)
    for zone, days in by_timezone.items():
        rebuild_affected_days(
            session,
            days=days,
            timezone=zone,
        )
    session.commit()
    return {
        "raw_events_deleted": report.raw_events_deleted,
        "summary_events_deleted": report.summary_events_deleted,
        "control_events_deleted": report.control_events_deleted,
        "compatibility_rows_deleted": report.compatibility_rows_deleted,
        "affected_dates": [value.isoformat() for value in report.affected_dates],
    }


@router.post("/v1/activity/maintenance")
def post_activity_maintenance(
    session: SessionDep,
    now: datetime | None = None,
) -> ActivityMaintenanceOut:
    if now is not None and now.tzinfo is None:
        raise APIError(422, "invalid_now", "now must include a timezone offset")
    report = run_activity_maintenance(
        session,
        now=now.astimezone(UTC) if now is not None else None,
    )
    session.commit()
    return ActivityMaintenanceOut(
        expired_events_deleted=report.expired_events_deleted,
        compatibility_rows_deleted=report.compatibility_rows_deleted,
        affected_dates=[value.isoformat() for value in report.affected_dates],
    )
