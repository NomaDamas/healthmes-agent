"""Unit tests for the open-wearables REST client (httpx.MockTransport only)."""

import logging

import httpx
import pytest

from healthmes.config import Settings
from healthmes.mcp_server.ow_client import (
    MAX_RESPONSE_BYTES,
    OWAuthError,
    OWClient,
    OWClientError,
    OWConfigurationError,
    OWNotFoundError,
    OWPayloadError,
    VendorWorkoutCollection,
)


class TrackingByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterations = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk


def _coverage_payload() -> dict:
    return {
        "providers": ["garmin", "whoop"],
        "timeseries": [
            {
                "name": "Heart & Cardiovascular",
                "metrics": [
                    {
                        "code": "heart_rate",
                        "unit": "bpm",
                        "providers": ["garmin", "whoop"],
                    }
                ],
            }
        ],
        "workout_fields": [
            {"code": "distance", "providers": ["garmin", "whoop"]}
        ],
        "sleep_fields": [
            {"code": "duration", "providers": ["garmin", "whoop"]}
        ],
        "health_scores": [
            {"code": "recovery", "providers": ["whoop"]}
        ],
    }


def _data_summary_payload(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "total_data_points": 5,
        "total_workouts": 2,
        "total_sleep_events": 1,
        "series_type_counts": {"heart_rate": 3, "steps": 2},
        "workout_type_counts": {"running": 2},
        "by_provider": [
            {
                "provider": "garmin",
                "data_points": 5,
                "series_counts": {"heart_rate": 3, "steps": 2},
                "workout_count": 2,
                "sleep_count": 1,
            }
        ],
        "has_womens_health_data": False,
    }


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

    async def test_debug_log_does_not_expose_provider_user_id(
        self, caplog, fake_ow, ow_client, ow_user_id
    ):
        with caplog.at_level(logging.DEBUG):
            await ow_client.get_sleep_summaries(
                ow_user_id,
                "2026-07-01",
                "2026-07-09",
            )
        assert ow_user_id not in caplog.text

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

    async def test_user_and_data_sources_paths_and_auth(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/data-sources"):
                return httpx.Response(200, json={"items": [], "total": 0})
            return httpx.Response(200, json={"id": ow_user_id})

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        user = await client.get_user(ow_user_id)
        sources = await client.get_user_data_sources(ow_user_id)

        assert requests[0].url.path == f"/api/v1/users/{ow_user_id}"
        assert requests[1].url.path == f"/api/v1/users/{ow_user_id}/data-sources"
        assert requests[0].headers["X-Open-Wearables-API-Key"] == ow_api_key
        assert user["id"] == ow_user_id
        assert sources == {"items": [], "total": 0}

    async def test_connections_path_and_list_response(self, ow_user_id, ow_api_key):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=[{"provider": "oura", "status": "active", "last_synced_at": None}],
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        connections = await client.get_connections(ow_user_id)

        assert requests[-1].url.path == f"/api/v1/users/{ow_user_id}/connections"
        assert connections[0]["provider"] == "oura"

    async def test_body_and_data_summary_route_signatures(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/summaries/body"):
                return httpx.Response(
                    200,
                    content=b"null",
                    headers={"Content-Type": "application/json"},
                )
            return httpx.Response(
                200,
                json=_data_summary_payload(ow_user_id),
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        body = await client.get_body_summary(
            ow_user_id,
            average_period=3,
            latest_window_hours=12,
        )
        data = await client.get_data_summary(
            ow_user_id,
            start_date="2026-07-01",
            end_date="2026-07-09",
        )

        body_request, data_request = requests
        assert body_request.url.path == (
            f"/api/v1/users/{ow_user_id}/summaries/body"
        )
        assert dict(body_request.url.params) == {
            "average_period": "3",
            "latest_window_hours": "12",
        }
        assert body is None
        assert data_request.url.path == (
            f"/api/v1/users/{ow_user_id}/summaries/data"
        )
        assert dict(data_request.url.params) == {
            "start_date": "2026-07-01",
            "end_date": "2026-07-09",
        }
        assert data["by_provider"][0]["provider"] == "garmin"

    async def test_provider_metadata_route_signatures(self, ow_api_key):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/meta/coverage"):
                return httpx.Response(200, json=_coverage_payload())
            return httpx.Response(
                200,
                json=[{"provider": "whoop", "is_enabled": True}],
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        coverage = await client.get_provider_coverage()
        providers = await client.get_configured_providers(
            enabled_only=True,
            cloud_only=True,
        )

        coverage_request, providers_request = requests
        assert coverage_request.url.path == "/api/v1/meta/coverage"
        assert coverage_request.headers["X-Open-Wearables-API-Key"] == ow_api_key
        assert coverage == _coverage_payload()
        assert providers_request.url.path == "/api/v1/providers"
        assert dict(providers_request.url.params) == {
            "enabled_only": "true",
            "cloud_only": "true",
        }
        assert providers[0]["provider"] == "whoop"

    async def test_polar_vendor_workout_granular_options_are_sent(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/workouts/workout-7"):
                return httpx.Response(200, json={"id": "workout-7"})
            return httpx.Response(200, json=[{"id": "workout-7"}])

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        workouts = await client.get_vendor_workouts(
            "POLAR",
            ow_user_id,
            since=123,
            limit=25,
            offset=5,
            filter_by_modification_time=False,
            samples=True,
            zones=True,
            route=True,
            summary_start_time="2026-07-01T00:00:00Z",
            summary_end_time="2026-07-09T00:00:00Z",
        )
        detail = await client.get_vendor_workout_detail(
            "POLAR",
            ow_user_id,
            "workout-7",
            samples=True,
            zones=True,
            route=True,
        )

        list_request, detail_request = requests
        assert list_request.url.path == (
            f"/api/v1/providers/polar/users/{ow_user_id}/workouts"
        )
        assert dict(list_request.url.params) == {
            "samples": "true",
            "zones": "true",
            "route": "true",
        }
        assert workouts == [{"id": "workout-7"}]
        assert detail_request.url.path == (
            f"/api/v1/providers/polar/users/{ow_user_id}/workouts/workout-7"
        )
        assert dict(detail_request.url.params) == {
            "samples": "true",
            "zones": "true",
            "route": "true",
        }
        assert detail == {"id": "workout-7"}

    async def test_garmin_vendor_workout_uses_only_summary_window(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/workouts/activity-7"):
                return httpx.Response(200, json={"activityId": "activity-7"})
            return httpx.Response(200, json=[{"activityId": "activity-7"}])

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        workouts = await client.get_vendor_workouts(
            "GARMIN",
            ow_user_id,
            since=123,
            limit=25,
            offset=5,
            filter_by_modification_time=False,
            samples=True,
            zones=True,
            route=True,
            summary_start_time="2026-07-01T00:00:00Z",
            summary_end_time="2026-07-09T00:00:00Z",
        )
        detail = await client.get_vendor_workout_detail(
            "GARMIN",
            ow_user_id,
            "activity-7",
            samples=True,
            zones=True,
            route=True,
        )

        list_request, detail_request = requests
        assert list_request.url.path == (
            f"/api/v1/providers/garmin/users/{ow_user_id}/workouts"
        )
        assert dict(list_request.url.params) == {
            "summary_start_time": "2026-07-01T00:00:00Z",
            "summary_end_time": "2026-07-09T00:00:00Z",
        }
        assert workouts == [{"activityId": "activity-7"}]
        assert detail_request.url.path == (
            f"/api/v1/providers/garmin/users/{ow_user_id}/workouts/activity-7"
        )
        assert dict(detail_request.url.params) == {}
        assert detail == {"activityId": "activity-7"}

    async def test_suunto_vendor_workout_contract_and_detail_unwrap(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []
        workout_key = "suunto-workout-key-7"

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith(f"/workouts/{workout_key}"):
                return httpx.Response(
                    200,
                    json={
                        "error": None,
                        "payload": {
                            "workoutKey": workout_key,
                            "workoutId": 7,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "workoutKey": workout_key,
                            "workoutId": 7,
                        }
                    ]
                },
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        workouts = await client.get_vendor_workouts(
            "SUUNTO",
            ow_user_id,
            since=1_782_864_000_000,
            limit=25,
            offset=5,
            filter_by_modification_time=False,
            samples=True,
            zones=True,
            route=True,
            summary_start_time="2026-07-01T00:00:00Z",
            summary_end_time="2026-07-09T00:00:00Z",
        )
        detail = await client.get_vendor_workout_detail(
            "SUUNTO",
            ow_user_id,
            workout_key,
            samples=True,
            zones=True,
            route=True,
        )

        list_request, detail_request = requests
        assert list_request.url.path == (
            f"/api/v1/providers/suunto/users/{ow_user_id}/workouts"
        )
        assert dict(list_request.url.params) == {
            "since": "1782864000000",
            "limit": "25",
            "offset": "5",
            "filter_by_modification_time": "false",
        }
        assert workouts == {
            "payload": [
                {
                    "workoutKey": workout_key,
                    "workoutId": 7,
                }
            ]
        }
        assert detail_request.url.path == (
            f"/api/v1/providers/suunto/users/{ow_user_id}/workouts/{workout_key}"
        )
        assert dict(detail_request.url.params) == {}
        assert detail == {
            "workoutKey": workout_key,
            "workoutId": 7,
        }

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
    async def test_declared_oversize_response_is_rejected_before_read(
        self,
        ow_api_key,
    ):
        stream = TrackingByteStream([b'{"data": []}'])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Length": str(MAX_RESPONSE_BYTES + 1)
                },
                stream=stream,
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWClientError, match="byte limit"):
            await client.list_users()
        assert stream.iterations == 0

    async def test_chunked_oversize_response_stops_before_json_parse(
        self,
        ow_api_key,
    ):
        stream = TrackingByteStream(
            [
                b"x" * (MAX_RESPONSE_BYTES // 2),
                b"x" * (MAX_RESPONSE_BYTES // 2 + 1),
                b'{"unreachable": true}',
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWClientError, match="byte limit"):
            await client.list_users()
        assert stream.iterations == 2

    async def test_401_maps_to_auth_error(self, fake_ow):
        client = OWClient(
            base_url="http://open-wearables.test",
            api_key="wrong-key",
            transport=fake_ow.transport(),
        )
        with pytest.raises(OWAuthError):
            await client.list_users()

    async def test_404_maps_to_not_found(self, ow_client):
        provider_user_id = "provider-user-secret"
        with pytest.raises(OWNotFoundError) as exc_info:
            await ow_client._get(
                f"/api/v1/users/{provider_user_id}/health-scores"
            )
        assert provider_user_id not in str(exc_info.value)

    async def test_missing_api_key_fails_before_any_request(self, fake_ow):
        client = OWClient(
            base_url="http://open-wearables.test",
            api_key="",
            transport=fake_ow.transport(),
        )
        with pytest.raises(OWConfigurationError):
            await client.list_users()
        assert fake_ow.requests == []

    async def test_5xx_maps_to_redacted_client_error(self, ow_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )
        provider_user_id = "provider-user-secret"
        with pytest.raises(OWClientError) as exc_info:
            await client.get_sleep_summaries(
                provider_user_id,
                "2026-07-01",
                "2026-07-09",
            )
        assert provider_user_id not in str(exc_info.value)
        assert "open-wearables.test" not in str(exc_info.value)

    async def test_connections_rejects_non_list_response(self, ow_user_id, ow_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWClientError, match="invalid connections response") as exc:
            await client.get_connections(ow_user_id)
        assert type(exc.value) is OWClientError

    async def test_connections_rejects_non_object_rows(
        self, ow_user_id, ow_api_key
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["private-provider-user-id"])

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid connections response"):
            await client.get_connections(ow_user_id)

    async def test_connections_mixed_list_rejects_bad_row_as_payload_error(
        self, ow_user_id, ow_api_key
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"provider": "whoop", "status": "active"},
                    "private-provider-user-id",
                ],
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid connections response"):
            await client.get_connections(ow_user_id)

    @pytest.mark.parametrize(
        "row",
        [
            {"provider": "WHOOP", "status": "active"},
            {"provider": "whoop_alias", "status": "active"},
            {"provider": "whoop", "status": "ACTIVE"},
            {
                "provider": "whoop",
                "status": "active",
                "id": "not-a-uuid",
            },
            {
                "provider": "whoop",
                "status": "active",
                "user_id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
            },
            {
                "provider": "whoop",
                "status": "active",
                "linked_user_ids": ["NOT-A-UUID"],
            },
        ],
    )
    async def test_connections_rejects_malformed_rows(
        self, ow_user_id, ow_api_key, row
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[row])

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid connections response"):
            await client.get_connections(ow_user_id)

    async def test_connections_rejects_duplicate_ids(
        self, ow_user_id, ow_api_key
    ):
        connection_id = "3f0efad3-6acc-4ddd-9128-2be58dfb7556"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": connection_id,
                        "user_id": ow_user_id,
                        "provider": "whoop",
                        "status": "active",
                    },
                    {
                        "id": connection_id,
                        "user_id": ow_user_id,
                        "provider": "garmin",
                        "status": "active",
                    },
                ],
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid connections response"):
            await client.get_connections(ow_user_id)

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"items": "not-a-list", "total": 0},
            {"items": ["private-source-id"], "total": 1},
            {"items": [], "total": True},
            {"items": [], "total": -1},
        ],
    )
    async def test_data_sources_rejects_malformed_envelope(
        self, ow_user_id, ow_api_key, payload
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWClientError, match="invalid data sources response"):
            await client.get_user_data_sources(ow_user_id)

    async def test_data_sources_preserves_valid_vendored_shape(
        self, ow_user_id, ow_api_key
    ):
        source_id = "3f0efad3-6acc-4ddd-9128-2be58dfb7556"
        connection_id = "0611817f-24cc-4d20-8d86-e55b93e9402c"
        payload = {
            "items": [
                {
                    "id": source_id,
                    "user_id": ow_user_id,
                    "provider": "whoop",
                    "user_connection_id": connection_id,
                    "device_model": None,
                    "software_version": None,
                    "source": "whoop",
                    "device_type": None,
                    "original_source_name": None,
                    "display_name": "Whoop",
                }
            ],
            "total": 1,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        assert await client.get_user_data_sources(ow_user_id) == payload

    @pytest.mark.parametrize(
        "payload",
        [
            {
                "items": [
                    {
                        "id": "not-a-uuid",
                        "user_id": "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e",
                        "provider": "whoop",
                    }
                ],
                "total": 1,
            },
            {
                "items": [
                    {
                        "id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
                        "user_id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
                        "provider": "whoop",
                    }
                ],
                "total": 1,
            },
            {
                "items": [
                    {
                        "id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
                        "user_id": "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e",
                        "provider": "apple_health",
                    }
                ],
                "total": 1,
            },
            {
                "items": [
                    {
                        "id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
                        "user_id": "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e",
                        "provider": "whoop",
                    }
                ],
                "total": 2,
            },
            {
                "items": [
                    {
                        "id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
                        "user_id": "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e",
                        "provider": "whoop",
                    },
                    {
                        "id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
                        "user_id": "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e",
                        "provider": "whoop",
                    },
                ],
                "total": 2,
            },
        ],
    )
    async def test_data_sources_rejects_invalid_rows_and_totals(
        self, ow_user_id, ow_api_key, payload
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid data sources response"):
            await client.get_user_data_sources(ow_user_id)

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {
                **_coverage_payload(),
                "providers": ["whoop", "whoop"],
            },
            {
                **_coverage_payload(),
                "providers": ["apple_health", "whoop"],
            },
            {
                **_coverage_payload(),
                "timeseries": [
                    {
                        "name": "Heart",
                        "metrics": [
                            {
                                "code": "",
                                "unit": "bpm",
                                "providers": ["whoop"],
                            }
                        ],
                    }
                ],
            },
            {
                **_coverage_payload(),
                "workout_fields": [
                    {"code": "distance", "providers": ["polar"]}
                ],
            },
            {
                **_coverage_payload(),
                "health_scores": [
                    {"code": "made_up_score", "providers": ["whoop"]}
                ],
            },
        ],
    )
    async def test_provider_coverage_rejects_malformed_metadata(
        self, ow_api_key, payload
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid provider coverage"):
            await client.get_provider_coverage()

    async def test_provider_coverage_rejects_duplicate_metric_codes(
        self, ow_api_key
    ):
        payload = _coverage_payload()
        payload["timeseries"].append(
            {
                "name": "Duplicate",
                "metrics": [
                    {
                        "code": "heart_rate",
                        "unit": "bpm",
                        "providers": ["whoop"],
                    }
                ],
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid provider coverage"):
            await client.get_provider_coverage()

    async def test_data_summary_preserves_valid_vendored_shape(
        self, ow_user_id, ow_api_key
    ):
        payload = _data_summary_payload(ow_user_id)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        assert await client.get_data_summary(ow_user_id) == payload

    @pytest.mark.parametrize(
        "payload",
        [
            {
                **_data_summary_payload(
                    "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
                ),
                "user_id": "3f0efad3-6acc-4ddd-9128-2be58dfb7556",
            },
            {
                **_data_summary_payload(
                    "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
                ),
                "total_data_points": True,
            },
            {
                **_data_summary_payload(
                    "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
                ),
                "series_type_counts": {"": 5},
            },
            {
                **_data_summary_payload(
                    "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
                ),
                "by_provider": [
                    {
                        "provider": "garmin_connect",
                        "data_points": 5,
                        "series_counts": {"heart_rate": 3, "steps": 2},
                        "workout_count": 2,
                        "sleep_count": 1,
                    }
                ],
            },
            {
                **_data_summary_payload(
                    "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
                ),
                "by_provider": [
                    {
                        "provider": "garmin",
                        "data_points": 5,
                        "series_counts": {"heart_rate": 3, "steps": 2},
                        "workout_count": 2,
                        "sleep_count": 1,
                    },
                    {
                        "provider": "garmin",
                        "data_points": 0,
                        "series_counts": {},
                        "workout_count": 0,
                        "sleep_count": 0,
                    },
                ],
            },
            {
                **_data_summary_payload(
                    "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
                ),
                "total_workouts": 3,
            },
            {
                **_data_summary_payload(
                    "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
                ),
                "by_provider": [
                    {
                        "provider": "garmin",
                        "data_points": 4,
                        "series_counts": {"heart_rate": 3, "steps": 2},
                        "workout_count": 2,
                        "sleep_count": 1,
                    }
                ],
            },
        ],
    )
    async def test_data_summary_rejects_malformed_or_conflicting_counts(
        self, ow_user_id, ow_api_key, payload
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="invalid data summary"):
            await client.get_data_summary(ow_user_id)

    async def test_configured_providers_rejects_non_list_response(
        self, ow_api_key
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": []})

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWClientError, match="invalid providers response"):
            await client.get_configured_providers()

    @pytest.mark.parametrize(
        ("method_name", "response_json", "error"),
        [
            (
                "get_body_summary",
                [],
                "invalid body summary response",
            ),
            (
                "get_vendor_workout_detail",
                [],
                "invalid vendor workout detail response",
            ),
        ],
    )
    async def test_typed_reads_reject_wrong_return_shapes(
        self,
        ow_user_id,
        ow_api_key,
        method_name,
        response_json,
        error,
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_json)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWClientError, match=error):
            if method_name == "get_body_summary":
                await client.get_body_summary(ow_user_id)
            else:
                await client.get_vendor_workout_detail(
                    "garmin",
                    ow_user_id,
                    "workout-7",
                )

    async def test_transport_failure_is_redacted_for_metadata_reads(
        self, ow_user_id, ow_api_key
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                f"cannot connect for {ow_user_id}",
                request=request,
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(
            OWClientError,
            match=r"^open-wearables request failed \(transport\)$",
        ) as exc_info:
            await client.get_user_data_sources(ow_user_id)
        assert ow_user_id not in str(exc_info.value)
        assert "open-wearables.test" not in str(exc_info.value)

    async def test_suunto_workout_detail_rejects_error_envelope(
        self, ow_user_id, ow_api_key
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error": "workout not found",
                    "payload": {"workoutKey": "private-workout-key"},
                },
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="Suunto workout detail error"):
            await client.get_vendor_workout_detail(
                "suunto",
                ow_user_id,
                "suunto-workout-key-7",
            )

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {},
            {"error": None, "payload": None},
            {"error": None, "payload": [{"workoutKey": "workout-key-7"}]},
        ],
    )
    async def test_suunto_workout_detail_requires_single_mapping_payload(
        self, ow_user_id, ow_api_key, payload
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(OWPayloadError, match="Suunto workout detail"):
            await client.get_vendor_workout_detail(
                "suunto",
                ow_user_id,
                "suunto-workout-key-7",
            )


class TestClientSideBounds:
    @pytest.mark.parametrize(
        ("average_period", "latest_window_hours", "error"),
        [
            (0, 4, "average_period"),
            (8, 4, "average_period"),
            (7, 0, "latest_window_hours"),
            (7, 25, "latest_window_hours"),
        ],
    )
    async def test_body_summary_rejects_vendor_bounds_before_request(
        self,
        fake_ow,
        ow_client,
        ow_user_id,
        average_period,
        latest_window_hours,
        error,
    ):
        with pytest.raises(ValueError, match=error):
            await ow_client.get_body_summary(
                ow_user_id,
                average_period=average_period,
                latest_window_hours=latest_window_hours,
            )
        assert fake_ow.requests == []

    async def test_vendor_workout_limit_rejects_only_declared_upper_bound(
        self, fake_ow, ow_client, ow_user_id
    ):
        with pytest.raises(ValueError, match="limit"):
            await ow_client.get_vendor_workouts(
                "suunto",
                ow_user_id,
                limit=101,
            )
        assert fake_ow.requests == []


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

    async def test_collect_sleep_sessions_tracked_uses_priority_and_page_cap(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            cursor = request.url.params.get("cursor")
            return httpx.Response(
                200,
                json={
                    "data": [{"id": cursor or "p1"}],
                    "pagination": {
                        "next_cursor": "p2" if cursor is None else "p3",
                    },
                },
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        rows, truncated = await client.collect_sleep_sessions_tracked(
            ow_user_id,
            "2026-07-01",
            "2026-07-09",
            max_pages=2,
        )

        assert [row["id"] for row in rows] == ["p1", "p2"]
        assert truncated is True
        assert [
            request.url.params["filter_by_priority"] for request in requests
        ] == ["true", "true"]

    async def test_collect_vendor_workouts_marks_long_garmin_window_unverified(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=[{"activityId": 7}])

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        collection = await client.collect_vendor_workouts_tracked(
            "GARMIN",
            ow_user_id,
            start_time="2026-07-01T00:00:00Z",
            end_time="2026-07-09T00:00:00Z",
            samples=True,
            zones=True,
            route=True,
        )

        request = requests[-1]
        assert request.url.path == (
            f"/api/v1/providers/garmin/users/{ow_user_id}/workouts"
        )
        assert request.url.params["summary_start_time"] == (
            "2026-07-01T00:00:00Z"
        )
        assert request.url.params["summary_end_time"] == (
            "2026-07-09T00:00:00Z"
        )
        assert "samples" not in request.url.params
        assert "zones" not in request.url.params
        assert "route" not in request.url.params
        assert collection == VendorWorkoutCollection(
            rows=({"activityId": 7},),
            truncated=False,
            completeness_unverified=True,
        )

    async def test_collect_vendor_workouts_accepts_one_day_garmin_window(
        self, ow_user_id, ow_api_key
    ):
        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json=[{"activityId": 7}],
                )
            ),
        )

        collection = await client.collect_vendor_workouts_tracked(
            "garmin",
            ow_user_id,
            start_time="2026-07-01T00:00:00Z",
            end_time="2026-07-02T00:00:00Z",
        )

        assert collection == VendorWorkoutCollection(
            rows=({"activityId": 7},),
        )

    async def test_collect_vendor_workouts_pages_suunto_payload(
        self, ow_user_id, ow_api_key
    ):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            offset = int(request.url.params["offset"])
            count = 100 if offset == 0 else 1
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {"workoutId": offset + index}
                        for index in range(count)
                    ]
                },
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        collection = await client.collect_vendor_workouts_tracked(
            "suunto",
            ow_user_id,
            start_time="2026-07-01T00:00:00Z",
            end_time="2026-07-09T00:00:00Z",
            samples=True,
            zones=True,
            route=True,
        )

        assert len(collection.rows) == 101
        assert collection.truncated is False
        assert collection.completeness_unverified is False
        assert [request.url.params["offset"] for request in requests] == [
            "0",
            "100",
        ]
        assert requests[0].url.params["since"] == "1782864000000"
        assert "summary_end_time" not in requests[0].url.params
        assert "samples" not in requests[0].url.params
        assert "zones" not in requests[0].url.params
        assert "route" not in requests[0].url.params

    async def test_collect_vendor_workouts_marks_unreplayable_token_truncated(
        self, ow_user_id, ow_api_key
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "records": [{"id": "whoop-7"}],
                    "next_token": "private-next-page",
                },
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )

        collection = await client.collect_vendor_workouts_tracked(
            "whoop",
            ow_user_id,
            start_time="2026-07-01T00:00:00Z",
            end_time="2026-07-09T00:00:00Z",
        )

        assert collection == VendorWorkoutCollection(
            rows=({"id": "whoop-7"},),
            truncated=True,
        )

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

    async def test_discovery_rejects_malformed_user_rows(self, settings, monkeypatch):
        from healthmes.mcp_server.ow_client import resolve_single_user_id

        monkeypatch.delenv("HEALTHMES_OW_USER_ID", raising=False)

        class MalformedFake:
            def list_users(self, **kwargs):
                return {"items": ["private@example.com"]}

        with pytest.raises(LookupError, match="HEALTHMES_OW_USER_ID"):
            await resolve_single_user_id(MalformedFake(), settings)


class TestPaginationTruncationTracked:
    """Defect 8: the collectors must surface a truncated flag when the page cap
    stops a fetch with more data available (offset and cursor variants)."""

    async def test_collect_health_scores_tracked_flags_offset_truncation(
        self, fake_ow, ow_client, ow_user_id
    ):
        for minute in range(11):
            fake_ow.add_score("stress", "garmin", f"2026-07-08T08:{minute:02d}:00Z", 30 + minute)
        fake_ow.max_page_size = 1  # 10-page cap hit with an 11th row still available
        rows, truncated = await ow_client.collect_health_scores_tracked(
            ow_user_id, start_date="2026-07-08", end_date="2026-07-09"
        )
        assert len(rows) == 10
        assert truncated is True
        fake_ow.max_page_size = None  # now it drains fully
        rows, truncated = await ow_client.collect_health_scores_tracked(
            ow_user_id, start_date="2026-07-08", end_date="2026-07-09"
        )
        assert len(rows) == 11
        assert truncated is False

    async def test_collect_sleep_summaries_tracked_flags_cursor_truncation(
        self, ow_user_id, ow_api_key
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            cursor = request.url.params.get("cursor")
            n = int(cursor) if cursor else 0
            return httpx.Response(
                200,
                json={
                    "data": [{"date": f"2026-07-{n + 1:02d}"}],
                    "pagination": {"next_cursor": str(n + 1)},  # never terminates
                },
            )

        client = OWClient(
            base_url="http://open-wearables.test",
            api_key=ow_api_key,
            transport=httpx.MockTransport(handler),
        )
        rows, truncated = await client.collect_sleep_summaries_tracked(
            ow_user_id, "2026-07-01", "2026-07-09", max_pages=3
        )
        assert len(rows) == 3  # capped
        assert truncated is True  # a live cursor remained at the cap

    async def test_plain_collectors_still_return_only_rows(
        self, fake_ow, ow_client, ow_user_id
    ):
        # Backward-compat: the non-tracked API is unchanged for existing callers.
        fake_ow.add_score("stress", "garmin", "2026-07-08T08:00:00Z", 30)
        rows = await ow_client.collect_health_scores(
            ow_user_id, start_date="2026-07-08", end_date="2026-07-09"
        )
        assert isinstance(rows, list)
        assert rows[0]["value"] == 30
