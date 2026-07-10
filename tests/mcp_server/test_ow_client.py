"""Unit tests for the open-wearables REST client (httpx.MockTransport only)."""

import httpx
import pytest

from healthmes.config import Settings
from healthmes.mcp_server.ow_client import (
    OWAuthError,
    OWClient,
    OWConfigurationError,
    OWNotFoundError,
)


class TestRequestShape:
    async def test_health_scores_path_params_and_auth_header(
        self, fake_ow, ow_client, ow_user_id, ow_api_key
    ):
        await ow_client.get_health_scores(
            ow_user_id,
            start_date="2026-07-01",
            end_date="2026-07-09",
            category="stress",
            provider="garmin",
            limit=100,
            offset=10,
        )
        request = fake_ow.requests[-1]
        assert request.url.path == f"/api/v1/users/{ow_user_id}/health-scores"
        assert request.headers["X-Open-Wearables-API-Key"] == ow_api_key
        params = request.url.params
        assert params["start_date"] == "2026-07-01"
        assert params["end_date"] == "2026-07-09"
        assert params["category"] == "stress"
        assert params["provider"] == "garmin"
        assert params["limit"] == "100"
        assert params["offset"] == "10"

    async def test_summaries_and_events_paths(self, fake_ow, ow_client, ow_user_id):
        await ow_client.get_sleep_summaries(ow_user_id, "2026-07-01", "2026-07-09")
        assert fake_ow.requests[-1].url.path == f"/api/v1/users/{ow_user_id}/summaries/sleep"
        await ow_client.get_recovery_summaries(ow_user_id, "2026-07-01", "2026-07-09")
        assert fake_ow.requests[-1].url.path == f"/api/v1/users/{ow_user_id}/summaries/recovery"
        await ow_client.get_workouts(ow_user_id, "2026-07-01", "2026-07-09", record_type="running")
        request = fake_ow.requests[-1]
        assert request.url.path == f"/api/v1/users/{ow_user_id}/events/workouts"
        assert request.url.params["record_type"] == "running"

    async def test_timeseries_types_are_repeated_query_params(self, ow_user_id, ow_api_key):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": [], "pagination": {}, "metadata": {}})

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )
        await client.get_timeseries(
            ow_user_id,
            "2026-07-08T00:00:00Z",
            "2026-07-09T00:00:00Z",
            ["heart_rate", "oxygen_saturation"],
            resolution="5min",
        )
        request = requests[-1]
        assert request.url.path == f"/api/v1/users/{ow_user_id}/timeseries"
        assert request.url.params.get_list("types") == ["heart_rate", "oxygen_saturation"]
        assert request.url.params["resolution"] == "5min"
        assert request.url.params["start_time"] == "2026-07-08T00:00:00Z"
        assert request.url.params["end_time"] == "2026-07-09T00:00:00Z"

    async def test_list_users_uses_old_paginated_envelope(self, fake_ow, ow_client, ow_user_id):
        payload = await ow_client.list_users(limit=2)
        assert fake_ow.requests[-1].url.path == "/api/v1/users"
        assert payload["items"][0]["id"] == ow_user_id

    def test_from_settings_reads_base_url_and_secret_key(self):
        settings = Settings(
            ow_base_url="http://somewhere.test:9999/",
            ow_api_key="s3cret",
            _env_file=None,
        )
        client = OWClient.from_settings(settings)
        assert client.base_url == "http://somewhere.test:9999"
        assert client.headers["X-Open-Wearables-API-Key"] == "s3cret"


class TestErrorMapping:
    async def test_401_maps_to_auth_error(self, fake_ow):
        client = OWClient(
            base_url="http://open-wearables.test",
            api_key="wrong-key",
            transport=fake_ow.transport(),
        )
        with pytest.raises(OWAuthError):
            await client.list_users()

    async def test_404_maps_to_not_found(self, ow_client):
        with pytest.raises(OWNotFoundError):
            await ow_client._get("/api/v1/users/nonexistent/health-scores")

    async def test_missing_api_key_fails_before_any_request(self, fake_ow):
        client = OWClient(
            base_url="http://open-wearables.test",
            api_key="",
            transport=fake_ow.transport(),
        )
        with pytest.raises(OWConfigurationError):
            await client.list_users()
        assert fake_ow.requests == []

    async def test_5xx_raises_http_status_error(self, ow_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_users()


class TestPagination:
    async def test_collect_health_scores_follows_offset_pages(
        self, fake_ow, ow_client, ow_user_id
    ):
        for hour in range(5):
            fake_ow.add_score("stress", "garmin", f"2026-07-08T{8 + hour:02d}:00:00Z", 30 + hour)
        fake_ow.max_page_size = 2  # force 3 pages: 2 + 2 + 1
        rows = await ow_client.collect_health_scores(
            ow_user_id, start_date="2026-07-08", end_date="2026-07-09"
        )
        assert len(rows) == 5
        assert [row["value"] for row in rows] == [30, 31, 32, 33, 34]
        offsets = [
            request.url.params["offset"]
            for request in fake_ow.requests
            if request.url.path.endswith("/health-scores")
        ]
        assert offsets == ["0", "2", "4"]

    async def test_collect_cursor_follows_next_cursor_chain(self, ow_user_id, ow_api_key):
        pages = {
            None: {"data": [{"date": "2026-07-07"}], "pagination": {"next_cursor": "p2"}},
            "p2": {"data": [{"date": "2026-07-08"}], "pagination": {"next_cursor": None}},
        }
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            cursor = request.url.params.get("cursor")
            seen.append(cursor)
            return httpx.Response(200, json=pages[cursor])

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )
        rows = await client.collect_sleep_summaries(ow_user_id, "2026-07-01", "2026-07-09")
        assert [row["date"] for row in rows] == ["2026-07-07", "2026-07-08"]
        assert seen == [None, "p2"]

    async def test_collect_timeseries_drains_cursor_pages(
        self, fake_ow, ow_client, ow_user_id
    ):
        for minute in range(5):
            fake_ow.add_stress_sample(f"2026-07-08T08:{minute:02d}:00Z", 20 + minute)
        fake_ow.timeseries_page_size = 2  # force 3 pages: 2 + 2 + 1
        rows = await ow_client.collect_timeseries(
            ow_user_id,
            "2026-07-08T00:00:00Z",
            "2026-07-09T00:00:00Z",
            ["garmin_stress_level"],
        )
        assert [row["value"] for row in rows] == [20, 21, 22, 23, 24]
        cursors = [
            request.url.params.get("cursor")
            for request in fake_ow.requests
            if request.url.path.endswith("/timeseries")
        ]
        assert cursors == [None, "2", "4"]

    async def test_collect_timeseries_honors_max_pages_cap(
        self, fake_ow, ow_client, ow_user_id
    ):
        for minute in range(6):
            fake_ow.add_stress_sample(f"2026-07-08T08:{minute:02d}:00Z", 20 + minute)
        fake_ow.timeseries_page_size = 2
        rows = await ow_client.collect_timeseries(
            ow_user_id,
            "2026-07-08T00:00:00Z",
            "2026-07-09T00:00:00Z",
            ["garmin_stress_level"],
            max_pages=2,
        )
        assert len(rows) == 4  # 2 pages of 2, then the cap stops the loop

    async def test_collect_timeseries_tracked_reports_truncation(
        self, fake_ow, ow_client, ow_user_id
    ):
        for minute in range(6):
            fake_ow.add_stress_sample(f"2026-07-08T08:{minute:02d}:00Z", 20 + minute)
        fake_ow.timeseries_page_size = 2

        rows, truncated = await ow_client.collect_timeseries_tracked(
            ow_user_id,
            "2026-07-08T00:00:00Z",
            "2026-07-09T00:00:00Z",
            ["garmin_stress_level"],
            max_pages=2,
        )
        assert len(rows) == 4
        assert truncated is True  # a cursor remained when the cap hit

        rows, truncated = await ow_client.collect_timeseries_tracked(
            ow_user_id,
            "2026-07-08T00:00:00Z",
            "2026-07-09T00:00:00Z",
            ["garmin_stress_level"],
            max_pages=10,
        )
        assert len(rows) == 6
        assert truncated is False


class TestResolveSingleUserId:
    """The one shared user-resolution policy (settings -> env -> exactly-one)."""

    async def test_settings_pin_wins_without_discovery(self, fake_ow, ow_client, settings):
        from healthmes.mcp_server.ow_client import resolve_single_user_id

        pinned = settings.model_copy(update={"ow_user_id": "pinned-user"})
        assert await resolve_single_user_id(ow_client, pinned) == "pinned-user"
        assert fake_ow.requests == []

    async def test_env_var_wins_over_discovery(
        self, fake_ow, ow_client, settings, monkeypatch
    ):
        from healthmes.mcp_server.ow_client import resolve_single_user_id

        monkeypatch.setenv("HEALTHMES_OW_USER_ID", "env-user")
        assert await resolve_single_user_id(ow_client, settings) == "env-user"
        assert fake_ow.requests == []

    async def test_discovery_accepts_exactly_one_user(
        self, fake_ow, ow_client, ow_user_id, settings, monkeypatch
    ):
        from healthmes.mcp_server.ow_client import resolve_single_user_id

        monkeypatch.delenv("HEALTHMES_OW_USER_ID", raising=False)
        assert await resolve_single_user_id(ow_client, settings) == ow_user_id

    async def test_discovery_rejects_ambiguity_with_remedy(
        self, fake_ow, ow_client, settings, monkeypatch
    ):
        from healthmes.mcp_server.ow_client import resolve_single_user_id

        monkeypatch.delenv("HEALTHMES_OW_USER_ID", raising=False)
        fake_ow.users.append({"id": "partner-2"})
        with pytest.raises(LookupError, match="HEALTHMES_OW_USER_ID"):
            await resolve_single_user_id(ow_client, settings)

    async def test_sync_fake_clients_are_supported(self, settings, monkeypatch):
        from healthmes.mcp_server.ow_client import resolve_single_user_id

        monkeypatch.delenv("HEALTHMES_OW_USER_ID", raising=False)

        class SyncFake:
            def list_users(self, **kwargs):
                return {"items": [{"id": "sync-user"}], "total": 1}

        assert await resolve_single_user_id(SyncFake(), settings) == "sync-user"
