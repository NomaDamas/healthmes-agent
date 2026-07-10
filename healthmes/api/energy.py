"""Cognitive-energy forecast endpoint (docs/PLAN.md §3, Phase 2).

``GET /cognitive-energy/forecast?date=YYYY-MM-DD`` returns the 24 hourly score
windows of one (UTC) day with their full component breakdowns:

- windows the hourly scheduler job already persisted are returned verbatim
  (``source: "persisted"`` — they saw the signals live);
- the remaining hours are computed on demand (``source: "computed"``) —
  the plan's on-demand compute path. Future hours naturally drop the
  fragmentation term (behavior cannot be forecast) and renormalize.

Component contract (verified by tests/energy): for every ``ok`` window the
component contributions sum exactly to ``score_exact`` and ``score`` is its
rounded integer. Windows without any usable signal are honest
``insufficient_data`` (``score: null``) instead of a fabricated number.

The path is the plan-verbatim ``/cognitive-energy/forecast``; a hidden
``/v1/cognitive-energy/forecast`` alias keeps the ``/v1`` REST convention.
"""

import datetime as dt
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from healthmes.config import resolve_timezone
from healthmes.engine.cognitive_energy import STATUS_OK, CognitiveEnergyEngine, WindowSlot
from healthmes.mcp_server import interpret

__all__ = ["router", "get_energy_engine"]

router = APIRouter(tags=["cognitive-energy"])


def get_energy_engine(request: Request) -> CognitiveEnergyEngine:
    """Resolve the engine for this app.

    Tests (and future wiring) may pre-set ``app.state.energy_engine`` with
    injected collaborators; otherwise the engine is built from ``Settings``
    (localhost-native defaults, process-wide store session factory).
    """
    existing = getattr(request.app.state, "energy_engine", None)
    if existing is not None:
        return existing
    return CognitiveEnergyEngine(request.app.state.settings)


class EnergyComponentOut(BaseModel):
    """One factor term of the score formula (a decision-tree input node)."""

    name: str
    kind: str
    weight: float | None
    raw: dict[str, Any]
    contribution: float


class EnergyWindowOut(BaseModel):
    """One hourly score window."""

    window_start: dt.datetime
    window_end: dt.datetime
    source: Literal["persisted", "computed"]
    status: str
    score: int | None
    score_exact: float | None
    components: list[EnergyComponentOut]


class EnergyForecastOut(BaseModel):
    """Response of ``GET /cognitive-energy/forecast``."""

    date: dt.date
    status: str
    baseline_window_days: int
    windows: list[EnergyWindowOut]


def _window_out(slot: WindowSlot) -> EnergyWindowOut:
    return EnergyWindowOut(
        window_start=slot.window_start,
        window_end=slot.window_end,
        source=slot.source,  # type: ignore[arg-type]
        status=slot.status,
        score=slot.score,
        score_exact=slot.score_exact,
        components=[EnergyComponentOut.model_validate(item) for item in slot.components],
    )


@router.get("/cognitive-energy/forecast")
def get_cognitive_energy_forecast(
    request: Request, date: dt.date | None = None
) -> EnergyForecastOut:
    """Hourly cognitive-energy forecast for one day.

    ``date`` defaults to today in the *user's* timezone (same default as the
    MCP twin ``get_cognitive_energy_forecast``, so a morning briefing never
    mixes two different "todays"); the returned windows are the 24 UTC-hour
    windows of that date (v1 simplification — the MCP tool is the local-day
    windowed surface).
    """
    day = (
        date
        if date is not None
        else dt.datetime.now(resolve_timezone(request.app.state.settings)).date()
    )
    engine = get_energy_engine(request)
    slots = engine.forecast_day(day)
    any_ok = any(slot.status == STATUS_OK for slot in slots)
    return EnergyForecastOut(
        date=day,
        status=STATUS_OK if any_ok else interpret.STATUS_INSUFFICIENT,
        baseline_window_days=interpret.BASELINE_WINDOW_DAYS,
        windows=[_window_out(slot) for slot in slots],
    )


# /v1 alias for REST-convention consumers (same handler, hidden from OpenAPI).
router.add_api_route(
    "/v1/cognitive-energy/forecast",
    get_cognitive_energy_forecast,
    methods=["GET"],
    response_model=EnergyForecastOut,
    include_in_schema=False,
)
