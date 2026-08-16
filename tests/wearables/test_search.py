from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from healthmes.wearables.search import (
    MAX_WEARABLE_SEARCH_PAGES,
    MAX_WEARABLE_SEARCH_PAYLOAD_BYTES,
    MAX_WEARABLE_SEARCH_ROWS,
    BoundedOpenWearablesSearch,
    WearableSearchRequest,
    normalize_retained_wearable_timeseries,
    validate_wearable_search_request,
)

START = datetime(2026, 8, 10, tzinfo=UTC)
END = START + timedelta(days=1)


def test_retained_timeseries_exact_boundary_remains_unattributed() -> None:
    fetched = normalize_retained_wearable_timeseries(
        (
            {
                "timestamp": "2026-08-10T08:00:00+00:00",
                "series_type": "steps",
                "value": 10,
                "unit": "count",
                "provider": "apple_health",
                "provider_attribution": "source_exact_alias",
                "device": "Private Watch A",
            },
            {
                "timestamp": "2026-08-10T08:00:00+00:00",
                "series_type": "steps",
                "value": 20,
                "unit": "count",
                "provider": "apple_health",
                "provider_attribution": "source_exact_alias",
                "device": "Private Watch B",
            },
        ),
        series_type="steps",
        resolution="1hour",
        start=START,
        end=END,
    )

    assert [record["value"] for record in fetched.records] == [10, 20]
    assert [record["timestamp"] for record in fetched.records] == [
        "2026-08-10T08:00:00+00:00",
        "2026-08-10T08:00:00+00:00",
    ]
    encoded = json.dumps(fetched.records, sort_keys=True)
    assert "Private Watch" not in encoded
    assert "_healthmes_stream_key" not in encoded
    assert fetched.limitations == (
        "wearable_stream_attribution_unavailable",
    )


def test_retained_timeseries_preserves_verified_attribution_status() -> None:
    fetched = normalize_retained_wearable_timeseries(
        (
            {
                "timestamp": "2026-08-10T08:00:00+00:00",
                "series_type": "steps",
                "value": 10,
                "unit": "count",
                "provider": "apple_health",
                "provider_attribution": "source_exact_alias",
            },
        ),
        series_type="steps",
        resolution="1hour",
        start=START,
        end=END,
        stream_attribution_verified=True,
    )

    assert fetched.records[0]["timestamp"] == (
        "2026-08-10T08:00:00+00:00"
    )
    assert fetched.limitations == ()


def _request(
    capability: str,
    *,
    start: datetime = START,
    end: datetime = END,
    **parameters: str,
) -> WearableSearchRequest:
    return WearableSearchRequest(
        capability=capability,
        start=start,
        end=end,
        timezone="UTC",
        parameters=parameters,
    )


class SanitizingClient:
    async def get_health_scores(self, user_id, **_kwargs):
        assert user_id == "private-user-id"
        return {
            "data": [
                {
                    "id": "raw-score-id",
                    "user_id": user_id,
                    "data_source_id": "private-data-source",
                    "authorization": "Bearer secret-value",
                    "category": "stress",
                    "provider": "garmin",
                    "recorded_at": "2026-08-10T08:00:00Z",
                    "value": 42,
                    "qualifier": "balanced",
                    "components": {
                        "signal": {
                            "value": 41,
                            "qualifier": "normal",
                        },
                        "access_token": {
                            "value": 99,
                            "qualifier": "secret-value",
                        },
                    },
                }
            ],
            "pagination": {"has_more": False},
        }

    async def get_workouts(self, user_id, *_args, **_kwargs):
        assert user_id == "private-user-id"
        return {
            "data": [
                {
                    "id": "raw-workout-id",
                    "user_id": user_id,
                    "name": "Private route name",
                    "route": [{"latitude": 37.5, "longitude": 127.0}],
                    "type": "running",
                    "start_time": "2026-08-10T09:00:00Z",
                    "end_time": "2026-08-10T09:30:00Z",
                    "duration_seconds": 1800,
                    "source": {
                        "provider": "garmin",
                        "device": "Private device name",
                    },
                    "distance_meters": 5000,
                }
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }

    async def get_timeseries(self, user_id, *_args, **_kwargs):
        assert user_id == "private-user-id"
        return {
            "data": [
                {
                    "id": "raw-series-id",
                    "user_id": user_id,
                    "timestamp": "2026-08-10T10:00:05Z",
                    "type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                    "source": {
                        "provider": "apple_health",
                        "device": "Private Watch",
                    },
                    "latitude": 37.5,
                    "longitude": 127.0,
                }
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


async def test_search_sanitizes_identity_secrets_raw_ids_and_gps() -> None:
    search = BoundedOpenWearablesSearch(
        SanitizingClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    scores = await search(_request("wearable.health-scores"))
    workouts = await search(_request("wearable.workouts"))
    timeseries = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=9),
            end=START + timedelta(hours=11),
            series_type="heart_rate",
            resolution="1min",
        )
    )

    assert scores.records == (
        {
            "category": "stress",
            "recorded_at": "2026-08-10T08:00:00+00:00",
            "provider": "garmin",
            "provider_attribution": "declared",
            "value": 42,
            "qualifier": "balanced",
            "components": [
                {
                    "component": "signal",
                    "value": 41,
                    "qualifier": "normal",
                }
            ],
        },
    )
    assert workouts.records == (
        {
            "workout_type": "running",
            "start_time": "2026-08-10T09:00:00+00:00",
            "end_time": "2026-08-10T09:30:00+00:00",
            "provider": "garmin",
            "provider_attribution": "source_exact_alias",
            "duration_seconds": 1800,
            "distance_meters": 5000,
        },
    )
    assert timeseries.records == (
        {
            "timestamp": "2026-08-10T10:00:00+00:00",
            "series_type": "heart_rate",
            "value": 72,
            "unit": "bpm",
            "provider": "apple_health",
            "provider_attribution": "source_exact_alias",
        },
    )
    assert timeseries.limitations == (
        "wearable_stream_attribution_unavailable",
    )
    encoded = json.dumps(
        {
            "scores": scores.records,
            "workouts": workouts.records,
            "timeseries": timeseries.records,
        },
        sort_keys=True,
    )
    for forbidden in (
        "private-user-id",
        "private-data-source",
        "secret-value",
        "raw-score-id",
        "raw-workout-id",
        "raw-series-id",
        "Private route name",
        "Private device name",
        "Private Watch",
        "latitude",
        "longitude",
        "route",
    ):
        assert forbidden not in encoded


class RawResolutionClient:
    async def get_timeseries(self, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:00:05Z",
                    "type": "heart_rate",
                    "value": 70,
                    "unit": "bpm",
                    "data_source_id": "apple-heart-rate-stream",
                    "source": {"provider": "apple_health"},
                },
                {
                    "timestamp": "2026-08-10T10:00:55Z",
                    "type": "heart_rate",
                    "value": 80,
                    "unit": "bpm",
                    "data_source_id": "apple-heart-rate-stream",
                    "source": {"provider": "apple_health"},
                },
                {
                    "timestamp": "2026-08-10T10:01:05Z",
                    "type": "heart_rate",
                    "value": 76,
                    "unit": "bpm",
                    "data_source_id": "apple-heart-rate-stream",
                    "source": {"provider": "apple_health"},
                },
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


async def test_timeseries_enforces_requested_resolution_locally() -> None:
    search = BoundedOpenWearablesSearch(
        RawResolutionClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    result = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=10),
            end=START + timedelta(hours=11),
            series_type="heart_rate",
            resolution="1min",
        )
    )

    assert result.records == (
        {
            "timestamp": "2026-08-10T10:00:00+00:00",
            "series_type": "heart_rate",
            "value": 75,
            "unit": "bpm",
            "provider": "apple_health",
            "provider_attribution": "source_exact_alias",
        },
        {
            "timestamp": "2026-08-10T10:01:00+00:00",
            "series_type": "heart_rate",
            "value": 76,
            "unit": "bpm",
            "provider": "apple_health",
            "provider_attribution": "source_exact_alias",
        },
    )


class SummableResolutionClient:
    async def get_timeseries(self, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:00:05Z",
                    "type": "steps",
                    "value": 10,
                    "unit": "count",
                    "data_source_id": "apple-steps-stream",
                    "source": {"provider": "apple_health"},
                    "is_daily_total": False,
                },
                {
                    "timestamp": "2026-08-10T10:00:25Z",
                    "type": "steps",
                    "value": 20,
                    "unit": "count",
                    "data_source_id": "apple-steps-stream",
                    "source": {"provider": "apple_health"},
                    "is_daily_total": False,
                },
                {
                    "timestamp": "2026-08-10T10:00:45Z",
                    "type": "steps",
                    "value": 1000,
                    "unit": "count",
                    "data_source_id": "garmin-steps-stream",
                    "source": {"provider": "garmin"},
                    "is_daily_total": True,
                },
                {
                    "timestamp": "2026-08-10T10:00:50Z",
                    "type": "steps",
                    "value": 900,
                    "unit": "count",
                    "data_source_id": "garmin-steps-stream",
                    "source": {"provider": "garmin"},
                    "is_daily_total": True,
                },
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


async def test_timeseries_uses_sum_and_prefers_daily_totals_per_provider() -> None:
    search = BoundedOpenWearablesSearch(
        SummableResolutionClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    result = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=10),
            end=START + timedelta(hours=11),
            series_type="steps",
            resolution="1min",
        )
    )

    by_provider = {
        record["provider"]: record
        for record in result.records
    }
    assert by_provider == {
        "apple_health": {
            "timestamp": "2026-08-10T10:00:00+00:00",
            "series_type": "steps",
            "value": 30,
            "unit": "count",
            "provider": "apple_health",
            "provider_attribution": "source_exact_alias",
        },
        "garmin": {
            "timestamp": "2026-08-10T10:00:00+00:00",
            "series_type": "steps",
            "value": 1000,
            "unit": "count",
            "provider": "garmin",
            "provider_attribution": "source_exact_alias",
            "is_daily_total": True,
        },
    }


class SummaryClient:
    async def get_activity_summaries(self, *_args, **_kwargs):
        return {
            "data": [
                {
                    "date": "2026-08-10",
                    "source": {
                        "provider": "apple_health",
                        "device": "Private phone",
                    },
                    "steps": 8000,
                    "active_minutes": 60,
                    "heart_rate": {
                        "avg_bpm": 74,
                        "max_bpm": 130,
                        "device_id": "private-device",
                    },
                }
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }

    async def get_sleep_summaries(self, *_args, **_kwargs):
        return {
            "data": [
                {
                    "date": "2026-08-10",
                    "source": {"provider": "oura"},
                    "start_time": "2026-08-09T23:00:00Z",
                    "end_time": "2026-08-10T07:00:00Z",
                    "duration_minutes": 420,
                    "sessions": [{"id": "raw-session-id"}],
                    "stages": {
                        "deep_minutes": 80,
                        "rem_minutes": 90,
                    },
                }
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }

    async def get_recovery_summaries(self, *_args, **_kwargs):
        return {
            "data": [
                {
                    "date": "2026-08-10",
                    "source": {"provider": "whoop"},
                    "recovery_score": 83,
                    "resting_heart_rate_bpm": 54,
                    "connection_id": "private-connection",
                }
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


@pytest.mark.parametrize(
    ("summary_kind", "expected"),
    (
        (
            "activity",
            {
                "summary_kind": "activity",
                "date": "2026-08-10",
                "provider": "apple_health",
                "provider_attribution": "source_exact_alias",
                "steps": 8000,
                "active_minutes": 60,
                "heart_rate": {
                    "avg_bpm": 74,
                    "max_bpm": 130,
                },
            },
        ),
        (
            "sleep",
            {
                "summary_kind": "sleep",
                "date": "2026-08-10",
                "provider": "oura",
                "provider_attribution": "source_exact_alias",
                "start_time": "2026-08-09T23:00:00+00:00",
                "end_time": "2026-08-10T07:00:00+00:00",
                "duration_minutes": 420,
                "stages": {
                    "deep_minutes": 80,
                    "rem_minutes": 90,
                },
            },
        ),
        (
            "recovery",
            {
                "summary_kind": "recovery",
                "date": "2026-08-10",
                "provider": "whoop",
                "provider_attribution": "source_exact_alias",
                "resting_heart_rate_bpm": 54,
                "recovery_score": 83,
            },
        ),
    ),
)
async def test_search_supports_allowlisted_daily_summaries(
    summary_kind: str,
    expected: dict,
) -> None:
    search = BoundedOpenWearablesSearch(
        SummaryClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(
        _request(
            "wearable.summaries",
            summary_kind=summary_kind,
        )
    )

    assert fetched.records == (expected,)
    encoded = json.dumps(fetched.records, sort_keys=True)
    assert "Private phone" not in encoded
    assert "private-device" not in encoded
    assert "raw-session-id" not in encoded
    assert "private-connection" not in encoded


class PagedHealthScoreClient:
    def __init__(self, *, verbose: bool = False) -> None:
        self.calls: list[tuple[int, int]] = []
        self.verbose = verbose

    async def get_health_scores(
        self,
        _user_id,
        *,
        limit: int,
        offset: int,
        **_kwargs,
    ):
        self.calls.append((limit, offset))
        rows = []
        for index in range(offset, offset + limit):
            row = {
                "category": "stress",
                "provider": "garmin",
                "recorded_at": (
                    START + timedelta(minutes=index)
                ).isoformat(),
                "value": index,
            }
            if self.verbose:
                row["components"] = {
                    f"component_{component:02d}_" + "x" * 40: {
                        "value": component,
                        "qualifier": "q" * 64,
                    }
                    for component in range(16)
                }
            rows.append(row)
        return {
            "data": rows,
            "pagination": {"has_more": True},
        }


async def test_search_stops_at_three_pages_and_250_rows() -> None:
    client = PagedHealthScoreClient()
    search = BoundedOpenWearablesSearch(
        client,  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(_request("wearable.health-scores"))

    assert len(client.calls) == MAX_WEARABLE_SEARCH_PAGES
    assert client.calls == [(100, 0), (100, 100), (51, 200)]
    assert len(fetched.records) == MAX_WEARABLE_SEARCH_ROWS
    assert fetched.upstream_truncated is True
    assert fetched.limitations == (
        "wearable_upstream_page_limit_reached",
    )


class MalformedOffsetPaginationClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.rows: list[object] = [
            {
                "category": "stress",
                "provider": "garmin",
                "recorded_at": (START + timedelta(minutes=0)).isoformat(),
                "value": 0,
            },
            "malformed-row-1",
            {
                "category": "stress",
                "provider": "garmin",
                "recorded_at": (START + timedelta(minutes=2)).isoformat(),
                "value": 2,
            },
            17,
            {
                "category": "stress",
                "provider": "garmin",
                "recorded_at": (START + timedelta(minutes=4)).isoformat(),
                "value": 4,
            },
        ]

    async def get_health_scores(
        self,
        _user_id,
        *,
        limit: int,
        offset: int,
        **_kwargs,
    ):
        self.calls.append((limit, offset))
        page = self.rows[offset : offset + min(limit, 2)]
        return {
            "data": page,
            "pagination": {
                "has_more": offset + len(page) < len(self.rows),
            },
        }


async def test_offset_pagination_advances_by_raw_page_cardinality() -> None:
    client = MalformedOffsetPaginationClient()
    search = BoundedOpenWearablesSearch(
        client,  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(_request("wearable.health-scores"))

    assert client.calls == [(100, 0), (100, 2), (100, 4)]
    assert [record["value"] for record in fetched.records] == [0, 2, 4]
    assert fetched.upstream_truncated is False
    assert fetched.discarded_rows == 2
    assert fetched.limitations == ("wearable_rows_discarded",)


class FullyMalformedCursorPageClient:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def get_workouts(
        self,
        _user_id,
        *_args,
        cursor: str | None,
        **_kwargs,
    ):
        self.calls.append(cursor)
        if cursor is None:
            return {
                "data": ["malformed-row", 17],
                "pagination": {
                    "next_cursor": "page-2",
                    "has_more": True,
                },
            }
        assert cursor == "page-2"
        return {
            "data": [
                {
                    "type": "running",
                    "start_time": "2026-08-10T09:00:00Z",
                    "end_time": "2026-08-10T09:30:00Z",
                    "provider": "garmin",
                }
            ],
            "pagination": {
                "next_cursor": None,
                "has_more": False,
            },
        }


async def test_cursor_pagination_continues_after_fully_malformed_page() -> None:
    client = FullyMalformedCursorPageClient()
    search = BoundedOpenWearablesSearch(
        client,  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(_request("wearable.workouts"))

    assert client.calls == [None, "page-2"]
    assert [record["workout_type"] for record in fetched.records] == [
        "running"
    ]
    assert fetched.upstream_truncated is False
    assert fetched.discarded_rows == 2
    assert fetched.limitations == ("wearable_rows_discarded",)


class RepeatingCursorTimeseriesClient:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def get_timeseries(
        self,
        _user_id,
        *_args,
        cursor: str | None,
        **_kwargs,
    ):
        self.calls.append(cursor)
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:10:00Z",
                    "type": "steps",
                    "value": 10,
                    "unit": "count",
                    "provider": "apple",
                    "data_source_id": "trusted-sensor",
                }
            ],
            "pagination": {
                "next_cursor": "repeated-page",
                "has_more": True,
            },
        }


async def test_cursor_cycle_does_not_aggregate_repeated_page() -> None:
    client = RepeatingCursorTimeseriesClient()
    search = BoundedOpenWearablesSearch(
        client,  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=9),
            end=START + timedelta(hours=11),
            series_type="steps",
            resolution="1hour",
        )
    )

    assert client.calls == [None, "repeated-page"]
    assert [record["value"] for record in fetched.records] == [10]
    assert fetched.upstream_truncated is True
    assert fetched.limitations == (
        "wearable_upstream_page_limit_reached",
    )


class MissingOffsetPaginationClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.rows = [
            {
                "category": "stress",
                "provider": "garmin",
                "recorded_at": (
                    START + timedelta(minutes=index)
                ).isoformat(),
                "value": index,
            }
            for index in range(101)
        ]

    async def get_health_scores(
        self,
        _user_id,
        *,
        limit: int,
        offset: int,
        **_kwargs,
    ):
        self.calls.append((limit, offset))
        return {"data": self.rows[offset : offset + limit]}


async def test_offset_pagination_rejects_missing_pagination_metadata() -> None:
    client = MissingOffsetPaginationClient()
    search = BoundedOpenWearablesSearch(
        client,  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    with pytest.raises(ValueError, match="pagination"):
        await search(_request("wearable.health-scores"))

    assert client.calls == [(100, 0)]


class NonMappingCursorPaginationClient:
    async def get_workouts(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "type": "running",
                    "start_time": "2026-08-10T09:00:00Z",
                    "end_time": "2026-08-10T09:30:00Z",
                    "provider": "garmin",
                }
            ],
            "pagination": [],
        }


async def test_cursor_pagination_rejects_non_mapping_metadata() -> None:
    search = BoundedOpenWearablesSearch(
        NonMappingCursorPaginationClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    with pytest.raises(ValueError, match="pagination"):
        await search(_request("wearable.workouts"))


class InvalidCursorPaginationClient:
    def __init__(self, pagination: object) -> None:
        self.pagination = pagination

    async def get_workouts(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "type": "running",
                    "start_time": "2026-08-10T09:00:00Z",
                    "end_time": "2026-08-10T09:30:00Z",
                    "provider": "garmin",
                }
            ],
            "pagination": self.pagination,
        }


@pytest.mark.parametrize(
    "pagination",
    (
        {"next_cursor": None},
        {"has_more": False},
        {"next_cursor": None, "has_more": True},
        {"next_cursor": "page-2", "has_more": False},
        {"next_cursor": "", "has_more": True},
        {"next_cursor": None, "has_more": "false"},
    ),
)
async def test_cursor_pagination_rejects_incomplete_or_contradictory_contract(
    pagination: object,
) -> None:
    search = BoundedOpenWearablesSearch(
        InvalidCursorPaginationClient(  # type: ignore[arg-type]
            pagination
        ),
        lambda: "private-user-id",
    )

    with pytest.raises(ValueError, match="cursor pagination"):
        await search(_request("wearable.workouts"))


class OversizedPageClient:
    async def get_health_scores(self, _user_id, *, limit, **_kwargs):
        return {
            "data": [{} for _ in range(limit + 1)],
            "pagination": {"has_more": False},
        }

    async def get_workouts(
        self,
        _user_id,
        *_args,
        limit,
        **_kwargs,
    ):
        return {
            "data": [{} for _ in range(limit + 1)],
            "pagination": {"next_cursor": None, "has_more": False},
        }


@pytest.mark.parametrize(
    "capability",
    ("wearable.health-scores", "wearable.workouts"),
)
async def test_search_rejects_page_larger_than_requested_limit(
    capability: str,
) -> None:
    search = BoundedOpenWearablesSearch(
        OversizedPageClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    with pytest.raises(ValueError, match="requested row limit"):
        await search(_request(capability))


async def test_search_trims_sanitized_payload_to_180kb() -> None:
    client = PagedHealthScoreClient(verbose=True)
    search = BoundedOpenWearablesSearch(
        client,  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(_request("wearable.health-scores"))

    encoded = json.dumps(
        {"records": fetched.records},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert fetched.payload_trimmed is True
    assert len(fetched.records) < MAX_WEARABLE_SEARCH_ROWS
    assert len(encoded) <= MAX_WEARABLE_SEARCH_PAYLOAD_BYTES
    assert "wearable_payload_limit_reached" in fetched.limitations


@pytest.mark.parametrize(
    "wearable_request",
    (
        _request(
            "wearable.timeseries",
            end=START + timedelta(hours=1),
            series_type="heart_rate",
            resolution="raw",
        ),
        _request(
            "wearable.timeseries",
            end=START + timedelta(hours=1),
            series_type="latitude",
            resolution="1min",
        ),
        _request(
            "wearable.timeseries",
            end=START + timedelta(hours=6, seconds=1),
            series_type="heart_rate",
            resolution="1min",
        ),
        _request(
            "wearable.health-scores",
            end=START + timedelta(days=30, seconds=1),
        ),
    ),
)
def test_search_rejects_raw_gps_and_oversized_windows(
    wearable_request: WearableSearchRequest,
) -> None:
    with pytest.raises(ValueError):
        validate_wearable_search_request(wearable_request)


@pytest.mark.parametrize(
    "wearable_request",
    (
        _request(
            "wearable.health-scores",
            category="not-a-score",
        ),
        _request(
            "wearable.summaries",
            summary_kind="body",
        ),
        _request(
            "wearable.workouts",
            unexpected="value",
        ),
    ),
)
async def test_search_rejects_invalid_contract_before_user_resolution(
    wearable_request: WearableSearchRequest,
) -> None:
    resolver_calls = 0

    def resolve_user() -> str:
        nonlocal resolver_calls
        resolver_calls += 1
        return "private-user-id"

    search = BoundedOpenWearablesSearch(
        SanitizingClient(),  # type: ignore[arg-type]
        resolve_user,
    )

    with pytest.raises(ValueError):
        await search(wearable_request)

    assert resolver_calls == 0


class UserIdEchoClient:
    async def get_health_scores(self, user_id, **_kwargs):
        return {
            "data": [
                {
                    "category": "stress",
                    "provider": f"source-for-{user_id}",
                    "recorded_at": "2026-08-10T08:00:00Z",
                    "value": 42,
                }
            ],
            "pagination": {"has_more": False},
        }


async def test_search_redacts_user_id_echoed_in_provider_text() -> None:
    search = BoundedOpenWearablesSearch(
        UserIdEchoClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(_request("wearable.health-scores"))

    assert len(fetched.records) == 1
    assert fetched.records[0]["provider"] == "unknown"
    assert fetched.records[0]["provider_attribution"] == (
        "declared_unclassified"
    )
    assert "private-user-id" not in json.dumps(fetched.records)
    assert fetched.discarded_rows == 0
    assert fetched.limitations == (
        "wearable_provider_attribution_unavailable",
    )


class SourceProviderPrivacyClient:
    async def get_timeseries(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:00:00Z",
                    "type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                    "source": {
                        "provider":
                            "com.apple.health."
                            "ED447642-08FD-4E45-AF20-633C02C83170"
                    },
                },
                {
                    "timestamp": "2026-08-10T10:01:00Z",
                    "type": "heart_rate",
                    "value": 74,
                    "unit": "bpm",
                    "source": {"provider": "com.pineapple.health"},
                },
                {
                    "timestamp": "2026-08-10T10:02:00Z",
                    "type": "heart_rate",
                    "value": 76,
                    "unit": "bpm",
                    "source": {"provider": "notgarmin-device"},
                },
                {
                    "timestamp": "2026-08-10T10:03:00Z",
                    "type": "heart_rate",
                    "value": 78,
                    "unit": "bpm",
                    "source": {"provider": "org.googleless.fit"},
                },
                {
                    "timestamp": "2026-08-10T10:04:00Z",
                    "type": "heart_rate",
                    "value": 80,
                    "unit": "bpm",
                    "source": {"provider": "unknown"},
                },
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


async def test_search_does_not_guess_provider_from_source_identifiers() -> None:
    search = BoundedOpenWearablesSearch(
        SourceProviderPrivacyClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=9),
            end=START + timedelta(hours=11),
            series_type="heart_rate",
            resolution="1min",
        )
    )

    assert [record["provider"] for record in fetched.records] == [
        "unknown",
        "unknown",
        "unknown",
        "unknown",
        "unknown",
    ]
    assert {
        record["provider_attribution"]
        for record in fetched.records
    } == {"source_unclassified"}
    encoded = json.dumps(fetched.records, sort_keys=True)
    assert "com.apple.health" not in encoded
    assert "ED447642" not in encoded
    assert "pineapple" not in encoded
    assert "notgarmin" not in encoded
    assert "googleless" not in encoded
    assert fetched.discarded_rows == 0


class ProviderPrecedenceClient:
    async def get_timeseries(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:00:00Z",
                    "type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                    "provider": "garmin",
                    "source": {"provider": "apple"},
                },
                {
                    "timestamp": "2026-08-10T10:01:00Z",
                    "type": "heart_rate",
                    "value": 73,
                    "unit": "bpm",
                    "source": {"provider": "apple"},
                },
                {
                    "timestamp": "2026-08-10T10:02:00Z",
                    "type": "heart_rate",
                    "value": 74,
                    "unit": "bpm",
                    "source": {"provider": "google"},
                },
                {
                    "timestamp": "2026-08-10T10:03:00Z",
                    "type": "heart_rate",
                    "value": 75,
                    "unit": "bpm",
                    "source": {"provider": "samsung"},
                },
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


async def test_search_uses_declared_provider_and_exact_legacy_aliases() -> None:
    search = BoundedOpenWearablesSearch(
        ProviderPrecedenceClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=9),
            end=START + timedelta(hours=11),
            series_type="heart_rate",
            resolution="1min",
        )
    )

    assert [
        (
            record["provider"],
            record["provider_attribution"],
        )
        for record in fetched.records
    ] == [
        ("garmin", "declared"),
        ("apple_health", "source_exact_alias"),
        ("google_health_connect", "source_exact_alias"),
        ("samsung_health", "source_exact_alias"),
    ]


class DistinctSensorStreamsClient:
    async def get_timeseries(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:00:05Z",
                    "type": "steps",
                    "value": 10,
                    "unit": "count",
                    "source": {
                        "provider": "apple",
                        "device": "device-A",
                    },
                },
                {
                    "timestamp": "2026-08-10T10:00:25Z",
                    "type": "steps",
                    "value": 20,
                    "unit": "count",
                    "source": {
                        "provider": "apple",
                        "device": "device-B",
                    },
                },
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


async def test_timeseries_never_sums_distinct_sensor_streams() -> None:
    search = BoundedOpenWearablesSearch(
        DistinctSensorStreamsClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=9),
            end=START + timedelta(hours=11),
            series_type="steps",
            resolution="1hour",
        )
    )

    assert [record["value"] for record in fetched.records] == [10, 20]
    assert [record["timestamp"] for record in fetched.records] == [
        "2026-08-10T10:00:00+00:00",
        "2026-08-10T10:00:00+00:00",
    ]
    assert all(
        record["provider"] == "apple_health"
        and record["provider_attribution"] == "source_exact_alias"
        for record in fetched.records
    )
    encoded = json.dumps(fetched.records, sort_keys=True)
    assert "device-A" not in encoded
    assert "device-B" not in encoded
    assert "_healthmes_stream_key" not in encoded
    assert fetched.limitations == (
        "wearable_stream_attribution_unavailable",
    )


class AmbiguousSensorStreamClient:
    async def get_timeseries(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:00:05Z",
                    "type": "steps",
                    "value": 10,
                    "unit": "count",
                    "source": {
                        "provider": "apple",
                        "device": "same-public-label",
                    },
                },
                {
                    "timestamp": "2026-08-10T10:00:25Z",
                    "type": "steps",
                    "value": 20,
                    "unit": "count",
                    "source": {
                        "provider": "apple",
                        "device": "same-public-label",
                    },
                },
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


async def test_timeseries_does_not_sum_ambiguous_matching_sources() -> None:
    search = BoundedOpenWearablesSearch(
        AmbiguousSensorStreamClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=9),
            end=START + timedelta(hours=11),
            series_type="steps",
            resolution="1min",
        )
    )

    assert [record["value"] for record in fetched.records] == [10, 20]
    assert [record["timestamp"] for record in fetched.records] == [
        "2026-08-10T10:00:00+00:00",
        "2026-08-10T10:00:00+00:00",
    ]
    assert fetched.limitations == (
        "wearable_stream_attribution_unavailable",
    )
    encoded = json.dumps(fetched.records, sort_keys=True)
    assert "same-public-label" not in encoded
    assert "_healthmes_stream_key" not in encoded


class MissingProviderClient:
    async def get_health_scores(self, _user_id, **_kwargs):
        return {
            "data": [
                {
                    "category": "stress",
                    "recorded_at": "2026-08-10T08:00:00Z",
                    "value": 42,
                }
            ],
            "pagination": {"has_more": False},
        }

    async def get_activity_summaries(
        self,
        _user_id,
        *_args,
        **_kwargs,
    ):
        return {
            "data": [{"date": "2026-08-10", "steps": 8000}],
            "pagination": {"next_cursor": None, "has_more": False},
        }

    async def get_workouts(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "type": "running",
                    "start_time": "2026-08-10T09:00:00Z",
                    "end_time": "2026-08-10T09:30:00Z",
                }
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }

    async def get_timeseries(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-10T10:00:00Z",
                    "type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                }
            ],
            "pagination": {"next_cursor": None, "has_more": False},
        }


@pytest.mark.parametrize(
    "wearable_request",
    (
        _request("wearable.health-scores"),
        _request(
            "wearable.summaries",
            summary_kind="activity",
        ),
        _request("wearable.workouts"),
        _request(
            "wearable.timeseries",
            start=START + timedelta(hours=9),
            end=START + timedelta(hours=11),
            series_type="heart_rate",
            resolution="1min",
        ),
    ),
)
async def test_search_retains_rows_without_provider_as_unknown(
    wearable_request: WearableSearchRequest,
) -> None:
    search = BoundedOpenWearablesSearch(
        MissingProviderClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )

    fetched = await search(wearable_request)

    assert len(fetched.records) == 1
    assert fetched.records[0]["provider"] == "unknown"
    assert fetched.records[0]["provider_attribution"] == "missing"
    assert fetched.discarded_rows == 0
    expected_limitations = {
        "wearable_provider_attribution_unavailable",
    }
    if wearable_request.capability == "wearable.timeseries":
        expected_limitations.add(
            "wearable_stream_attribution_unavailable"
        )
    assert set(fetched.limitations) == expected_limitations
