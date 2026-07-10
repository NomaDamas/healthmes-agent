"""Insight list + deterministic recompute (docs/PLAN.md Phase 1 + Phase 2).

``POST /v1/insights/recompute`` runs the fixed correlation templates over one
period — the four Phase-1 stress templates of
:mod:`healthmes.api.insight_templates` plus the Phase-2 focus template of
:mod:`healthmes.api.insight_focus` (per-hour cognitive-energy dips joined with
app usage and calendar load). Stress samples and workouts are fetched
read-only from open-wearables through the **shared**
:class:`healthmes.mcp_server.ow_client.OWClient` (the same client the MCP
tools, trigger sweep and energy engine use — auth, paths and pagination live
in exactly one place); energy estimates, app-usage samples and calendar
events come from the local store. Hour/weekday bucketing happens in the
user's timezone (``Settings.timezone``, machine-local when unset). Recompute
is idempotent per period — existing rows for the managed template kinds are
replaced. Honesty guarantees: the timeseries fetch is sized to the whole
window and a truncated fetch fails loudly (502 ``upstream_truncated``)
instead of computing a confidently wrong insight over a partial window. No
LLM is involved; statements, evidence and confidence are pure functions of
the data.
"""

import asyncio
import uuid
import zoneinfo
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT, HTTP_502_BAD_GATEWAY

from healthmes.api.common import ensure_utc, utc_now
from healthmes.api.errors import APIError
from healthmes.api.insight_focus import KIND_FOCUS_DROP_BY_HOUR, compute_focus_drop
from healthmes.api.insight_templates import (
    ALL_KINDS,
    SkippedTemplate,
    StressSample,
    WorkoutEvent,
    compute_all,
)
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.config import Settings, resolve_timezone
from healthmes.mcp_server.ow_client import OWClient, OWClientError, resolve_single_user_id
from healthmes.store import (
    AppUsageSample,
    CalendarEventMirror,
    CognitiveEnergyEstimate,
    Insight,
)
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/insights", tags=["insights"])

DEFAULT_PERIOD_DAYS = 28
MAX_PERIOD_DAYS = 92

# Garmin is the only provider with a native stress series (docs/PLAN.md §1.5).
STRESS_SERIES_TYPE = "garmin_stress_level"

# Timeseries page budget per window day: raw Garmin stress arrives at up to
# ~1/min = 1440 rows/day = 15 pages at the route max limit of 100; doubled for
# headroom. The cap exists only to bound a pathological cursor loop — a
# genuine fetch that still hits it is reported as upstream_truncated rather
# than silently aggregated (the old fixed 10k-sample cap kept ~7 days of a
# 28-day window and produced confidently wrong insights).
_TIMESERIES_PAGES_PER_DAY = 30

# Every kind the recompute pipeline owns (idempotent replace per period).
MANAGED_KINDS: tuple[str, ...] = (*ALL_KINDS, KIND_FOCUS_DROP_BY_HOUR)


def get_ow_client(request: Request) -> OWClient:
    """Resolve the shared open-wearables client for this app.

    Tests (and future wiring) may pre-set ``app.state.ow_client`` (an
    ``OWClient``, e.g. over an ``httpx.MockTransport``); otherwise a client is
    built from ``Settings`` (localhost-native defaults).
    """
    existing = getattr(request.app.state, "ow_client", None)
    if existing is not None:
        return existing
    return OWClient.from_settings(request.app.state.settings)


class InsightOut(BaseModel):
    """Insight row as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period: str
    kind: str
    statement: str
    evidence: dict[str, Any] | None
    confidence: float | None
    created_at: datetime


class RecomputeRequest(BaseModel):
    """Request body of ``POST /v1/insights/recompute`` (all fields optional)."""

    period_start: date | None = None
    period_end: date | None = Field(
        default=None, description="Inclusive end date; defaults to today (UTC)."
    )
    ow_user_id: str | None = Field(
        default=None,
        max_length=64,
        description="open-wearables user id; defaults to the shared single-user "
        "resolution (Settings.ow_user_id / HEALTHMES_OW_USER_ID / discovery "
        "when the API key sees exactly one user).",
    )

    @model_validator(mode="after")
    def _check_range(self) -> "RecomputeRequest":
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("period_start must not be after period_end")
        return self


class SkippedOut(BaseModel):
    """A template skipped during recompute, with the reason."""

    kind: str
    reason: str


class RecomputeOut(BaseModel):
    """Result of one recompute run."""

    period: str
    ow_user_id: str
    insights: list[InsightOut]
    skipped: list[SkippedOut]


@router.get("")
def list_insights(
    session: SessionDep,
    page: PageParamsDep,
    period: str | None = None,
    kind: str | None = None,
) -> Page[InsightOut]:
    """List insights, newest first (filters: exact ``period`` string, ``kind``)."""
    stmt = select(Insight).order_by(Insight.created_at.desc(), Insight.kind)
    if period is not None:
        stmt = stmt.where(Insight.period == period)
    if kind is not None:
        stmt = stmt.where(Insight.kind == kind)
    rows, meta = paginate(session, stmt, page)
    return Page(data=[InsightOut.model_validate(row) for row in rows], pagination=meta)


def _resolve_period(body: RecomputeRequest) -> tuple[date, date]:
    period_end = body.period_end or utc_now().date()
    period_start = body.period_start or period_end - timedelta(days=DEFAULT_PERIOD_DAYS - 1)
    if (period_end - period_start).days + 1 > MAX_PERIOD_DAYS:
        raise APIError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_range",
            f"recompute period must be at most {MAX_PERIOD_DAYS} days",
        )
    return period_start, period_end


def _user_timezone(settings: Settings) -> tzinfo:
    try:
        return resolve_timezone(settings)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise APIError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_timezone",
            f"configured timezone is not a valid IANA name: {exc}",
        ) from exc


def _parse_stress_samples(rows: list[dict[str, Any]]) -> list[StressSample]:
    """Rows -> normalized samples (negative values are Garmin 'unmeasurable')."""
    samples: list[StressSample] = []
    for row in rows:
        if row.get("type") != STRESS_SERIES_TYPE:
            continue
        raw_ts, raw_value = row.get("timestamp"), row.get("value")
        if raw_ts is None or raw_value is None:
            continue
        value = float(raw_value)
        if value < 0:
            continue
        timestamp = ensure_utc(datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")))
        samples.append(StressSample(timestamp=timestamp, value=value))
    samples.sort(key=lambda sample: sample.timestamp)
    return samples


def _parse_workouts(rows: list[dict[str, Any]]) -> list[WorkoutEvent]:
    events: list[WorkoutEvent] = []
    for row in rows:
        start_raw, end_raw = row.get("start_time"), row.get("end_time")
        if start_raw is None or end_raw is None:
            continue
        events.append(
            WorkoutEvent(
                workout_type=str(row.get("type") or "unknown"),
                start_time=ensure_utc(datetime.fromisoformat(str(start_raw))),
                end_time=ensure_utc(datetime.fromisoformat(str(end_raw))),
            )
        )
    events.sort(key=lambda event: event.start_time)
    return events


async def _fetch_ow_inputs(
    client: OWClient,
    settings: Settings,
    explicit_user_id: str | None,
    window_start: datetime,
    window_end: datetime,
    days: int,
) -> tuple[str, list[dict[str, Any]], bool, list[dict[str, Any]]]:
    user_id = explicit_user_id or await resolve_single_user_id(client, settings)
    series_rows, truncated = await client.collect_timeseries_tracked(
        user_id,
        window_start.isoformat(),
        window_end.isoformat(),
        [STRESS_SERIES_TYPE],
        max_pages=days * _TIMESERIES_PAGES_PER_DAY + 2,
    )
    if truncated:
        # The caller refuses to compute over partial data; skip the rest.
        return user_id, series_rows, True, []
    workout_rows = await client.collect_workouts(
        user_id, window_start.isoformat(), window_end.isoformat()
    )
    return user_id, series_rows, truncated, workout_rows


@router.post("/recompute")
def recompute_insights(
    body: RecomputeRequest, session: SessionDep, request: Request
) -> RecomputeOut:
    """Recompute the template insights (Phase-1 stress + Phase-2 focus) for a period.

    Idempotent per period: previous rows of every managed kind are replaced.
    """
    period_start, period_end = _resolve_period(body)
    days = (period_end - period_start).days + 1
    period = f"{period_start.isoformat()}..{period_end.isoformat()}"
    window_start = datetime.combine(period_start, datetime.min.time(), tzinfo=UTC)
    window_end = datetime.combine(
        period_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )
    settings: Settings = request.app.state.settings
    tz = _user_timezone(settings)

    ow = get_ow_client(request)
    try:
        # Sync route handlers run in the threadpool (no running loop), so the
        # shared async client is driven to completion here.
        ow_user_id, series_rows, truncated, workout_rows = asyncio.run(
            _fetch_ow_inputs(ow, settings, body.ow_user_id, window_start, window_end, days)
        )
    except LookupError as exc:
        raise APIError(
            HTTP_422_UNPROCESSABLE_CONTENT, "ow_user_unresolved", str(exc)
        ) from exc
    except (OWClientError, httpx.HTTPError) as exc:
        raise APIError(
            HTTP_502_BAD_GATEWAY, "upstream_error", f"open-wearables: {exc}"
        ) from exc
    if truncated:
        raise APIError(
            HTTP_502_BAD_GATEWAY,
            "upstream_truncated",
            "the stress timeseries fetch exceeded the page budget for this "
            "window; refusing to compute insights over partial data — narrow "
            "the period and retry",
        )
    samples = _parse_stress_samples(series_rows)
    workouts = _parse_workouts(workout_rows)

    events = list(
        session.scalars(
            select(CalendarEventMirror)
            .where(
                CalendarEventMirror.end_at > window_start,
                CalendarEventMirror.start_at < window_end,
            )
            .order_by(CalendarEventMirror.start_at)
        ).all()
    )
    estimates = list(
        session.scalars(
            select(CognitiveEnergyEstimate)
            .where(
                CognitiveEnergyEstimate.window_start >= window_start,
                CognitiveEnergyEstimate.window_start < window_end,
            )
            .order_by(CognitiveEnergyEstimate.window_start)
        ).all()
    )
    usage = list(
        session.scalars(
            select(AppUsageSample)
            .where(
                AppUsageSample.bucket_start >= window_start,
                AppUsageSample.bucket_start < window_end,
            )
            .order_by(AppUsageSample.bucket_start)
        ).all()
    )

    results, skipped = compute_all(samples, events, workouts, tz)
    focus = compute_focus_drop(estimates, usage, events, tz)
    if isinstance(focus, SkippedTemplate):
        skipped.append(focus)
    else:
        results.append(focus)

    # Idempotent per period: replace previous rows of the managed kinds.
    session.execute(
        delete(Insight).where(Insight.period == period, Insight.kind.in_(MANAGED_KINDS))
    )
    rows = [
        Insight(
            period=period,
            kind=result.kind,
            statement=result.statement,
            evidence=result.evidence,
            confidence=result.confidence,
        )
        for result in results
    ]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)

    return RecomputeOut(
        period=period,
        ow_user_id=ow_user_id,
        insights=[InsightOut.model_validate(row) for row in rows],
        skipped=[SkippedOut(kind=item.kind, reason=item.reason) for item in skipped],
    )
