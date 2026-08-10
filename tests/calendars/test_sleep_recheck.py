from datetime import date

import pytest

from healthmes.calendars.sleep_recheck import recheck_sleep_night
from healthmes.mcp_server.ow_client import OWAuthError, OWClientError, OWConfigurationError


class FakeReader:
    def __init__(self, rows_by_date, *, connections=None, error=None):
        self.rows_by_date = rows_by_date
        self.connections = connections if connections is not None else [
            {"provider": "oura", "status": "active"}
        ]
        self.error = error

    async def list_users(self, **_kwargs):
        return {"items": [{"id": "only-user"}]}

    async def get_connections(self, _user_id):
        if self.error is not None:
            raise self.error
        return self.connections

    async def collect_sleep_summaries(self, _user_id, start_date, _end_date):
        if self.error is not None:
            raise self.error
        return self.rows_by_date.get(start_date, [])


def valid_row(day: str) -> dict[str, object]:
    return {
        "date": day,
        "source": {"provider": "oura"},
        "start_time": f"{day}T22:00:00+00:00",
        "end_time": f"{day}T23:00:00+00:00",
        "duration_minutes": 60,
        "time_in_bed_minutes": 60,
    }


@pytest.mark.anyio
async def test_recheck_reports_date_basis_mismatch_without_writing(settings) -> None:
    result = await recheck_sleep_night(
        settings,
        date(2026, 7, 26),
        client=FakeReader({"2026-07-27": [valid_row("2026-07-27")]}),
    )
    assert result["outcome"] == "date_basis_mismatch"
    assert result["calendar_write"] == "unchanged"
    assert result["windows"] == {
        "night_start": {"start": "2026-07-26", "end": "2026-07-27"},
        "oura_summary": {"start": "2026-07-27", "end": "2026-07-28"},
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (OWAuthError("no"), "authentication_failure"),
        (OWConfigurationError("no"), "authentication_failure"),
        (OWClientError("no"), "api_transport_failure"),
    ],
)
async def test_recheck_redacts_client_failures(settings, error, outcome) -> None:
    result = await recheck_sleep_night(
        settings, date(2026, 7, 26), client=FakeReader({}, error=error)
    )
    assert result["outcome"] == outcome
    assert result["calendar_write"] == "unchanged"
