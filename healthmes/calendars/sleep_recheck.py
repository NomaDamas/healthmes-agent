from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Protocol

from healthmes.calendars.sleep_observation import ActualSleepObservation, SleepObservationNoOp
from healthmes.calendars.sleep_source import read_actual_sleep
from healthmes.config import Settings
from healthmes.mcp_server.ow_client import (
    OWAuthError,
    OWClient,
    OWClientError,
    OWConfigurationError,
    resolve_single_user_id,
)


class SleepRecheckReader(Protocol):
    async def list_users(
        self, *, search: str | None = None, limit: int = 100
    ) -> Mapping[str, Any]: ...
    async def get_connections(self, user_id: str) -> Sequence[object]: ...
    async def collect_sleep_summaries(
        self, user_id: str, start_date: str, end_date: str
    ) -> Sequence[Mapping[str, Any]]: ...


async def recheck_sleep_night(
    settings: Settings,
    night_start: date,
    *,
    client: SleepRecheckReader | None = None,
) -> dict[str, object]:
    reader = client or OWClient.from_settings(settings)
    windows = {
        "night_start": _window(night_start),
        "oura_summary": _window(night_start + timedelta(days=1)),
    }
    try:
        user_id = await resolve_single_user_id(reader, settings)
        connections = await reader.get_connections(user_id)
    except (OWAuthError, OWConfigurationError):
        return _result("authentication_failure", windows)
    except OWClientError:
        return _result("api_transport_failure", windows)
    except LookupError:
        return _result("authentication_failure", windows)
    if not _oura_is_active(connections):
        return _result("vendor_sync_failure", windows)
    try:
        night = await read_actual_sleep(reader, user_id, night_start)
        summary = await read_actual_sleep(reader, user_id, night_start + timedelta(days=1))
    except (OWAuthError, OWConfigurationError):
        return _result("authentication_failure", windows)
    except OWClientError:
        return _result("api_transport_failure", windows)
    if isinstance(night, ActualSleepObservation):
        return _result("ok", windows, selected_date=night.local_date.isoformat())
    if isinstance(summary, ActualSleepObservation):
        return _result("date_basis_mismatch", windows, selected_date=summary.local_date.isoformat())
    if _incomplete(night, summary):
        return _result("incomplete_record", windows)
    return _result("no_provider_record", windows)


def _window(day: date) -> dict[str, str]:
    return {"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()}


def _oura_is_active(connections: Sequence[object]) -> bool:
    return any(
        isinstance(row, Mapping)
        and str(row.get("provider", "")).strip().lower() == "oura"
        and str(row.get("status", "")).strip().lower() == "active"
        for row in connections
    )


def _incomplete(*observations: SleepObservationNoOp | ActualSleepObservation) -> bool:
    return any(
        isinstance(observation, SleepObservationNoOp)
        and observation.reason.value in {"incomplete", "nap_only", "ambiguous"}
        for observation in observations
    )


def _result(
    outcome: str,
    windows: dict[str, dict[str, str]],
    *,
    selected_date: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "read_only_recheck",
        "outcome": outcome,
        "windows": windows,
        "calendar_write": "unchanged",
    }
    if selected_date is not None:
        result["selected_date"] = selected_date
    return result
