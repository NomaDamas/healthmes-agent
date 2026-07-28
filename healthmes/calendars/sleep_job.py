from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import anyio
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars.approval import ApprovalCalendar, calendar_approval_target
from healthmes.calendars.base import CalendarBackend
from healthmes.calendars.jobs import _build_backend, write_source
from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    SleepObservationNoOp,
    SleepObservationNoOpReason,
    SleepSummaryPayload,
    select_actual_sleep,
)
from healthmes.calendars.sleep_preview import preview_sleep_reconciliation
from healthmes.calendars.sleep_proposals import prepare_sleep_proposal
from healthmes.calendars.sleep_reconciliation import SleepCalendarReconciler
from healthmes.config import Settings, resolve_timezone
from healthmes.mcp_server.ow_client import (
    OWClient,
    OWClientError,
    resolve_single_user_id,
)
from healthmes.store.enums import CalendarSource
from healthmes.store.session import session_scope

logger = logging.getLogger(__name__)
RECENT_SLEEP_WINDOW_DAYS = 3


class SleepReconciliationError(RuntimeError):
    pass


class SleepSummaryReader(Protocol):
    async def collect_sleep_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
    ) -> Sequence[Mapping[str, object]]: ...


def build_sleep_reconciliation_job(
    settings: Settings,
    *,
    client: SleepSummaryReader | None = None,
    backend_factory: Callable[[], CalendarBackend] | None = None,
    session_factory: sessionmaker[Session] | None = None,
    date_provider: Callable[[dt.tzinfo], dt.date] | None = None,
) -> Callable[[], dict[str, object] | None] | None:
    calendar_source = write_source(settings)
    if calendar_source is None:
        return None
    active_client = client if client is not None else OWClient.from_settings(settings)
    backend: CalendarBackend | None = None
    resolved_user_id = settings.ow_user_id
    local_timezone = resolve_timezone(settings)

    async def run_once() -> dict[str, object]:
        nonlocal backend, resolved_user_id
        if resolved_user_id is None:
            if not isinstance(active_client, OWClient):
                raise RuntimeError("injected sleep reader requires an explicit ow_user_id")
            resolved_user_id = await resolve_single_user_id(active_client, settings)
        if backend is None:
            backend = (
                backend_factory()
                if backend_factory is not None
                else _build_backend(settings, calendar_source)
            )
        target_date = (
            date_provider(local_timezone)
            if date_provider is not None
            else dt.datetime.now(local_timezone).date()
        )
        return await prepare_recent_sleep_window(
            end_date=target_date,
            calendar_source=calendar_source,
            client=active_client,
            user_id=resolved_user_id,
            session_factory=session_factory,
            calendar=ApprovalCalendar(
                backend,
                calendar_approval_target(settings, calendar_source),
            ),
        )

    def run_job() -> dict[str, object] | None:
        try:
            return anyio.run(run_once)
        except Exception as exc:
            logger.error(
                "Actual sleep reconciliation failed (%s); next interval will retry.",
                type(exc).__name__,
            )
            return None

    return run_job


async def prepare_recent_sleep_window(
    *,
    end_date: dt.date,
    calendar_source: CalendarSource,
    client: SleepSummaryReader,
    user_id: str,
    session_factory: sessionmaker[Session] | None,
    calendar: ApprovalCalendar,
) -> dict[str, object]:
    start_date = end_date - dt.timedelta(days=RECENT_SLEEP_WINDOW_DAYS - 1)
    results: list[dict[str, object]] = []
    for offset in range(RECENT_SLEEP_WINDOW_DAYS):
        with session_scope(session_factory) as session:
            proposal = await prepare_sleep_proposal(
                target_date=start_date + dt.timedelta(days=offset),
                calendar_source=calendar_source,
                reader=client,
                user_id=user_id,
                session=session,
                calendar=calendar,
            )
            results.append(
                {
                    **proposal.snapshot,
                    "proposal_id": str(proposal.id),
                    "proposal_status": proposal.status.value,
                }
            )
    return {
        "status": "ok",
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        "results": results,
    }


async def preview_recent_sleep(
    settings: Settings,
    target_date: dt.date | None = None,
) -> dict[str, object]:
    calendar_source = write_source(settings)
    if calendar_source is None:
        raise SleepReconciliationError(
            "no configured calendar; connect Google or iCloud before dry-run"
        )
    client = OWClient.from_settings(settings)
    try:
        user_id = await resolve_single_user_id(client, settings)
    except (LookupError, OWClientError) as exc:
        raise SleepReconciliationError(str(exc)) from exc
    timezone = resolve_timezone(settings)
    day = target_date or dt.datetime.now(timezone).date()
    backend = _build_backend(settings, calendar_source)
    try:
        return await reconcile_recent_sleep(
            target_date=day,
            calendar_source=calendar_source,
            client=client,
            user_id=user_id,
            session_factory=None,
            backend=backend,
            dry_run=True,
        )
    except OWClientError as exc:
        raise SleepReconciliationError(
            f"open-wearables sleep data unavailable ({type(exc).__name__})"
        ) from None


async def reconcile_recent_sleep_window(
    *,
    end_date: dt.date,
    calendar_source: CalendarSource,
    client: SleepSummaryReader,
    user_id: str,
    session_factory: sessionmaker[Session] | None,
    backend: CalendarBackend,
) -> dict[str, object]:
    start_date = end_date - dt.timedelta(days=RECENT_SLEEP_WINDOW_DAYS - 1)
    results = []
    for offset in range(RECENT_SLEEP_WINDOW_DAYS):
        results.append(
            await reconcile_recent_sleep(
                target_date=start_date + dt.timedelta(days=offset),
                calendar_source=calendar_source,
                client=client,
                user_id=user_id,
                session_factory=session_factory,
                backend=backend,
            )
        )
    return {
        "status": "ok",
        "window_start": start_date.isoformat(),
        "window_end": end_date.isoformat(),
        "results": results,
    }


async def reconcile_recent_sleep(
    *,
    target_date: dt.date,
    calendar_source: CalendarSource,
    client: SleepSummaryReader,
    user_id: str,
    session_factory: sessionmaker[Session] | None,
    backend: CalendarBackend | None,
    dry_run: bool = False,
) -> dict[str, object]:
    end_date = target_date + dt.timedelta(days=1)
    rows = await client.collect_sleep_summaries(
        user_id,
        target_date.isoformat(),
        end_date.isoformat(),
    )
    try:
        summaries = tuple(SleepSummaryPayload.model_validate(row) for row in rows)
    except ValidationError:
        selected: ActualSleepObservation | SleepObservationNoOp = SleepObservationNoOp(
            reason=SleepObservationNoOpReason.INCOMPLETE
        )
    else:
        selected = select_actual_sleep(summaries, target_date)
    if isinstance(selected, SleepObservationNoOp):
        return {
            "status": "noop",
            "reason": selected.reason.value,
            "calendar": calendar_source.value,
            "local_date": target_date.isoformat(),
        }

    with session_scope(session_factory) as session:
        if dry_run and backend is None:
            raise ValueError("backend is required for an exact dry-run")
        preview = preview_sleep_reconciliation(
            session,
            calendar_source,
            selected,
            backend,
        )
        if dry_run:
            return preview
        if preview["action"] == "blocked":
            return {**preview, "status": "blocked"}
        if backend is None:
            raise ValueError("backend is required when dry_run is false")
        if backend.source is not calendar_source:
            raise ValueError("backend source does not match target calendar")
        result = SleepCalendarReconciler(session, backend).reconcile(selected)
        response: dict[str, object] = {
            **preview,
            "status": "ok",
            "action": result.action.value,
            "planned_sleep_replacements": len(result.deleted_planned_external_ids),
        }
        if result.planned_sleep_cleanup_pending:
            response["status"] = "cleanup_pending"
            response["planned_sleep_cleanup_pending"] = (
                result.planned_sleep_cleanup_pending
            )
        return response
