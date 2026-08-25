from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings
from healthmes.mcp_server.ow_client import OWClientError
from healthmes.store import InputSourcePolicy
from healthmes.wearables import (
    OPEN_WEARABLES_DISCONNECTED,
    OPEN_WEARABLES_METADATA_DEGRADED,
    OPEN_WEARABLES_METADATA_UNAVAILABLE,
    OPEN_WEARABLES_PROVIDER_BINDING_CHANGED,
    OPEN_WEARABLES_SOURCE_POLICY_CHANGED,
    OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE,
    OPEN_WEARABLES_UNCONFIGURED,
    WEARABLE_INPUT_DISABLED,
    OpenWearablesAvailabilityResolver,
    OpenWearablesAvailabilitySnapshot,
    OpenWearablesAvailabilityState,
    persist_open_wearables_snapshot,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
CONNECTION_ID = "7a6b1a1e-2f6d-4a5b-9c3e-1f2a3b4c5d6e"
DATA_SOURCE_ID = "3f0efad3-6acc-4ddd-9128-2be58dfb7556"


def _coverage() -> dict[str, Any]:
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
                    },
                    {
                        "code": "heart_rate_variability_rmssd",
                        "unit": "ms",
                        "providers": ["whoop"],
                    },
                ],
            },
            {
                "name": "Provider-Specific",
                "metrics": [
                    {
                        "code": "garmin_stress_level",
                        "unit": "",
                        "providers": ["garmin"],
                    }
                ],
            },
        ],
        "workout_fields": [
            {"code": "distance", "providers": ["garmin", "whoop"]}
        ],
        "sleep_fields": [
            {"code": "duration", "providers": ["garmin", "whoop"]}
        ],
        "health_scores": [
            {"code": "day_strain", "providers": ["whoop"]},
            {"code": "recovery", "providers": ["whoop"]},
            {"code": "stress", "providers": ["garmin"]},
        ],
    }


def _inventory(
    *,
    providers: tuple[str, ...] = (),
) -> dict[str, Any]:
    rows = []
    for provider in providers:
        series_counts = (
            {
                "heart_rate": 3,
                "heart_rate_variability_rmssd": 2,
            }
            if provider == "whoop"
            else {
                "garmin_stress_level": 2,
                "heart_rate": 3,
            }
        )
        rows.append(
            {
                "provider": provider,
                "data_points": sum(series_counts.values()),
                "series_counts": series_counts,
                "workout_count": 1,
                "sleep_count": 1,
            }
        )
    return {
        "user_id": "user-1",
        "total_data_points": sum(
            row["data_points"] for row in rows
        ),
        "total_workouts": len(rows),
        "total_sleep_events": len(rows),
        "series_type_counts": {
            code: sum(
                row["series_counts"].get(code, 0)
                for row in rows
            )
            for code in {
                code
                for row in rows
                for code in row["series_counts"]
            }
        },
        "workout_type_counts": (
            {"running": len(rows)} if rows else {}
        ),
        "by_provider": rows,
        "has_womens_health_data": False,
    }


def _active_connection(
    provider: str,
    *,
    connection_id: str = CONNECTION_ID,
) -> dict[str, Any]:
    return {
        "id": connection_id,
        "provider": provider,
        "status": "active",
    }


def _data_source(
    provider: str,
    *,
    connection_id: str | None = CONNECTION_ID,
    source_id: str = DATA_SOURCE_ID,
) -> dict[str, Any]:
    return {
        "id": source_id,
        "provider": provider,
        "user_connection_id": connection_id,
    }


class FakeOpenWearablesClient:
    def __init__(
        self,
        *,
        users: list[dict] | None = None,
        connections: list[dict] | None = None,
        data_sources: dict | None = None,
        coverage: dict | None = None,
        inventory: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        self.users = users if users is not None else [{"id": "user-1"}]
        self.connections = connections or []
        self.data_sources = data_sources or {"items": [], "total": 0}
        self.coverage = coverage if coverage is not None else _coverage()
        self.inventory = (
            inventory if inventory is not None else _inventory()
        )
        self.error = error
        self.health_reads = 0

    async def list_users(self, *, limit: int) -> dict:
        assert limit == 2
        if self.error is not None:
            raise self.error
        return {"items": self.users}

    async def get_connections(self, user_id: str) -> list[dict]:
        assert user_id == "user-1"
        if self.error is not None:
            raise self.error
        return self.connections

    async def get_user_data_sources(self, user_id: str) -> dict:
        assert user_id == "user-1"
        if self.error is not None:
            raise self.error
        return self.data_sources

    async def get_provider_coverage(self) -> dict:
        if self.error is not None:
            raise self.error
        return self.coverage

    async def get_data_summary(self, user_id: str) -> dict:
        assert user_id == "user-1"
        if self.error is not None:
            raise self.error
        return self.inventory


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "ow_base_url": "https://open-wearables.test",
        "ow_api_key": "test-key",
        "decision_owner_principal_id": "owner",
    }
    values.update(overrides)
    return Settings(
        **values,
    )


def _resolver(
    session_factory: sessionmaker[Session],
    client: FakeOpenWearablesClient,
    *,
    settings: Settings | None = None,
) -> OpenWearablesAvailabilityResolver:
    return OpenWearablesAvailabilityResolver(
        settings=settings or _settings(),
        client=client,  # type: ignore[arg-type]
        session_factory=session_factory,
        clock=lambda: NOW,
    )


def _persist_retained_binding(
    session_factory: sessionmaker[Session],
    snapshot: OpenWearablesAvailabilitySnapshot,
) -> None:
    assert snapshot.provider_binding_digest is not None
    with session_factory() as session:
        persist_open_wearables_snapshot(
            session,
            normalized_context={
                "date": NOW.date().isoformat(),
                "timezone": "UTC",
                "charge": {"value": 78},
                "hrv": {"value": 52},
                "yesterday_load": {"value": 11},
                "source_refs": [
                    {
                        "source_provider": "open-wearables",
                        "upstream_provider": "whoop",
                        "record_id": "whoop-recovery-1",
                    }
                ],
            },
            local_day=NOW.date(),
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
            provider_binding_digest=snapshot.provider_binding_digest,
        )
        session.commit()


@pytest.mark.asyncio
async def test_source_disabled_short_circuits_metadata_reads(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add(
            InputSourcePolicy(
                owner_principal_id="owner",
                source_id="wearable.open-wearables",
                enabled=False,
                revision=1,
            )
        )
        session.commit()
    client = FakeOpenWearablesClient(error=AssertionError("metadata read"))

    snapshot = await _resolver(session_factory, client)()

    assert snapshot.state is OpenWearablesAvailabilityState.DISABLED
    assert snapshot.reason_codes == (WEARABLE_INPUT_DISABLED,)
    assert snapshot.blocking_reason_code == WEARABLE_INPUT_DISABLED
    assert snapshot.exposes_capabilities is False
    assert snapshot.source_policy_revision == 1


@pytest.mark.asyncio
async def test_missing_api_configuration_is_unconfigured(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeOpenWearablesClient(error=AssertionError("metadata read"))

    snapshot = await _resolver(
        session_factory,
        client,
        settings=_settings(ow_api_key=""),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.UNCONFIGURED
    assert snapshot.reason_codes == (OPEN_WEARABLES_UNCONFIGURED,)


@pytest.mark.asyncio
async def test_unresolvable_user_is_unconfigured(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(users=[]),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.UNCONFIGURED
    assert snapshot.blocking_reason_code == OPEN_WEARABLES_UNCONFIGURED


@pytest.mark.asyncio
async def test_no_connection_or_data_source_is_disconnected(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.DISCONNECTED
    assert snapshot.reason_codes == (OPEN_WEARABLES_DISCONNECTED,)


@pytest.mark.asyncio
async def test_connection_without_bound_data_source_is_disconnected(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=[_active_connection("whoop")],
        ),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.DISCONNECTED
    assert snapshot.reason_codes == (OPEN_WEARABLES_DISCONNECTED,)


@pytest.mark.asyncio
async def test_matching_connection_data_source_and_coverage_are_available(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=[_active_connection("whoop")],
            data_sources={
                "items": [_data_source("whoop")],
                "total": 1,
            },
            inventory=_inventory(providers=("whoop",)),
        ),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.AVAILABLE
    assert snapshot.providers == ("whoop",)
    assert snapshot.provider_binding_digest is not None
    assert snapshot.exposes_capabilities is True
    assert snapshot.providers_for(
        "wearable.whoop-recovery-package"
    ) == {"whoop"}
    assert snapshot.providers_for("wearable.body-summary") == {
        "whoop"
    }
    assert snapshot.providers_for("wearable.provider-workouts") == set()
    assert snapshot.parameter_values(
        "wearable.health-scores",
        "category",
    ) == {"day_strain", "recovery"}
    binding = snapshot.capability_binding("wearable.timeseries")
    assert binding is not None
    assert binding.providers_for_value(
        "series_type",
        "heart_rate",
    ) == {"whoop"}
    assert binding.providers_for_value(
        "series_type",
        "garmin_stress_level",
    ) == set()


@pytest.mark.asyncio
async def test_multiple_providers_expose_only_value_level_ownership(
    session_factory: sessionmaker[Session],
) -> None:
    garmin_connection = (
        "8b7c2b2f-3a7e-4b6c-8d4f-2a3b4c5d6e7f"
    )
    garmin_source = "4a1fcbe4-7bdd-5eee-ae23-3cf69egc8667".replace(
        "g",
        "f",
    )
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=[
                _active_connection("whoop"),
                _active_connection(
                    "garmin",
                    connection_id=garmin_connection,
                ),
            ],
            data_sources={
                "items": [
                    _data_source("whoop"),
                    _data_source(
                        "garmin",
                        connection_id=garmin_connection,
                        source_id=garmin_source,
                    ),
                ],
                "total": 2,
            },
            inventory=_inventory(
                providers=("whoop", "garmin")
            ),
        ),
    )()

    assert snapshot.providers == ("garmin", "whoop")
    timeseries = snapshot.capability_binding("wearable.timeseries")
    assert timeseries is not None
    assert timeseries.providers_for_value(
        "series_type",
        "heart_rate",
    ) == {"garmin", "whoop"}
    assert timeseries.providers_for_value(
        "series_type",
        "garmin_stress_level",
    ) == {"garmin"}
    scores = snapshot.capability_binding("wearable.health-scores")
    assert scores is not None
    assert scores.providers_for_value(
        "category",
        "recovery",
    ) == {"whoop"}
    assert scores.providers_for_value(
        "category",
        "stress",
    ) == {"garmin"}
    assert snapshot.parameter_values(
        "wearable.provider-workouts",
        "provider",
    ) == {"garmin"}
    assert snapshot.providers_for("wearable.body-summary") == {
        "whoop"
    }


@pytest.mark.asyncio
async def test_body_summary_is_hidden_when_inventory_spans_providers(
    session_factory: sessionmaker[Session],
) -> None:
    garmin_connection = (
        "8b7c2b2f-3a7e-4b6c-8d4f-2a3b4c5d6e7f"
    )
    garmin_source = "4a1fcbe4-7bdd-5eee-ae23-3cf69efc8667"
    inventory = _inventory(providers=("whoop", "garmin"))
    inventory["by_provider"][1]["series_counts"][
        "resting_heart_rate"
    ] = 1
    inventory["by_provider"][1]["data_points"] += 1
    inventory["total_data_points"] += 1
    inventory["series_type_counts"]["resting_heart_rate"] = 1
    coverage = _coverage()
    coverage["timeseries"][0]["metrics"].append(
        {
            "code": "resting_heart_rate",
            "unit": "bpm",
            "providers": ["garmin", "whoop"],
        }
    )

    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=[
                _active_connection("whoop"),
                _active_connection(
                    "garmin",
                    connection_id=garmin_connection,
                ),
            ],
            data_sources={
                "items": [
                    _data_source("whoop"),
                    _data_source(
                        "garmin",
                        connection_id=garmin_connection,
                        source_id=garmin_source,
                    ),
                ],
                "total": 2,
            },
            coverage=coverage,
            inventory=inventory,
        ),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.AVAILABLE
    assert snapshot.providers_for("wearable.body-summary") == set()


@pytest.mark.asyncio
async def test_detached_import_requires_positive_inventory(
    session_factory: sessionmaker[Session],
) -> None:
    source = {
        "items": [
            _data_source("whoop", connection_id=None)
        ],
        "total": 1,
    }

    empty = await _resolver(
        session_factory,
        FakeOpenWearablesClient(data_sources=source),
    )()
    imported = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            data_sources=source,
            inventory=_inventory(providers=("whoop",)),
        ),
    )()

    assert empty.state is OpenWearablesAvailabilityState.DISCONNECTED
    assert imported.state is OpenWearablesAvailabilityState.AVAILABLE
    assert imported.provider_bindings[0].imported is True
    assert imported.provider_bindings[0].direct_api is False


@pytest.mark.asyncio
async def test_imported_only_whoop_does_not_expose_live_recovery_package(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            data_sources={
                "items": [_data_source("whoop", connection_id=None)],
                "total": 1,
            },
            inventory=_inventory(providers=("whoop",)),
        ),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.AVAILABLE
    assert snapshot.provider_bindings[0].imported is True
    assert snapshot.provider_bindings[0].direct_api is False
    assert snapshot.providers_for("wearable.whoop-recovery-package") == set()
    assert snapshot.providers_for("wearable.recovery") == {"whoop"}


@pytest.mark.asyncio
async def test_metadata_read_off_on_revision_race_is_fail_closed(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add(
            InputSourcePolicy(
                owner_principal_id="owner",
                source_id="wearable.open-wearables",
                enabled=True,
                revision=1,
            )
        )
        session.commit()

    class BlockingMetadataClient(FakeOpenWearablesClient):
        def __init__(self) -> None:
            super().__init__(
                connections=[
                    _active_connection("whoop")
                ],
                data_sources={
                    "items": [_data_source("whoop")],
                    "total": 1,
                },
                inventory=_inventory(providers=("whoop",)),
            )
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def get_connections(self, user_id: str) -> list[dict]:
            assert user_id == "user-1"
            self.started.set()
            await self.release.wait()
            return self.connections

        async def get_user_data_sources(self, user_id: str) -> dict:
            assert user_id == "user-1"
            await self.release.wait()
            return self.data_sources

    client = BlockingMetadataClient()
    pending = asyncio.create_task(_resolver(session_factory, client)())
    await client.started.wait()

    with session_factory() as session:
        policy = session.query(InputSourcePolicy).one()
        policy.enabled = False
        policy.revision = 2
        session.commit()
    with session_factory() as session:
        policy = session.query(InputSourcePolicy).one()
        policy.enabled = True
        policy.revision = 3
        session.commit()

    client.release.set()
    snapshot = await pending

    assert snapshot.state is OpenWearablesAvailabilityState.DISABLED
    assert snapshot.source_policy_revision == 3
    assert snapshot.reason_codes == (
        OPEN_WEARABLES_SOURCE_POLICY_CHANGED,
    )
    assert (
        snapshot.blocking_reason_code
        == OPEN_WEARABLES_SOURCE_POLICY_CHANGED
    )
    assert snapshot.exposes_capabilities is False


@pytest.mark.asyncio
async def test_revoked_connection_cannot_be_revived_by_stale_data_source(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=[
                {
                    "id": CONNECTION_ID,
                    "provider": "whoop",
                    "status": "revoked",
                }
            ],
            data_sources={
                "items": [
                    _data_source("whoop")
                ],
                "total": 1,
            },
            inventory=_inventory(providers=("whoop",)),
        ),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.DISCONNECTED
    assert snapshot.reason_codes == (OPEN_WEARABLES_DISCONNECTED,)


@pytest.mark.asyncio
async def test_metadata_transport_failure_without_retained_data_is_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(error=OWClientError("offline")),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.UNAVAILABLE
    assert snapshot.reason_codes == (OPEN_WEARABLES_METADATA_UNAVAILABLE,)
    assert snapshot.blocking_reason_code == OPEN_WEARABLES_METADATA_UNAVAILABLE
    assert snapshot.exposes_capabilities is False


@pytest.mark.asyncio
async def test_legacy_retained_data_without_lkg_is_not_degraded(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        persist_open_wearables_snapshot(
            session,
            normalized_context={
                "date": NOW.date().isoformat(),
                "timezone": "UTC",
                "stress": {"value": 42},
            },
            local_day=NOW.date(),
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
        )
        session.commit()

    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(error=OWClientError("offline")),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.UNAVAILABLE
    assert snapshot.reason_codes == (OPEN_WEARABLES_METADATA_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_matching_lkg_and_retained_binding_allow_degraded_mode(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeOpenWearablesClient(
        connections=[_active_connection("whoop")],
        data_sources={
            "items": [_data_source("whoop")],
            "total": 1,
        },
        inventory=_inventory(providers=("whoop",)),
    )
    resolver = _resolver(session_factory, client)
    available = await resolver()
    _persist_retained_binding(session_factory, available)
    client.error = OWClientError("offline")

    degraded = await resolver()

    assert degraded.state is OpenWearablesAvailabilityState.DEGRADED
    assert degraded.reason_codes == (OPEN_WEARABLES_METADATA_DEGRADED,)
    assert degraded.provider_binding_digest == (
        available.provider_binding_digest
    )
    assert degraded.capability_catalog == (
        available.capability_binding("wearable.recovery"),
    )
    assert degraded.providers == ("whoop",)
    assert degraded.blocking_reason_code is None


@pytest.mark.asyncio
async def test_same_coverage_with_inventory_failure_allows_degraded_mode(
    session_factory: sessionmaker[Session],
) -> None:
    class InventoryFailureClient(FakeOpenWearablesClient):
        fail_inventory = False

        async def get_data_summary(self, user_id: str) -> dict:
            if self.fail_inventory:
                raise OWClientError("inventory unavailable")
            return await super().get_data_summary(user_id)

    client = InventoryFailureClient(
        connections=[_active_connection("whoop")],
        data_sources={
            "items": [_data_source("whoop")],
            "total": 1,
        },
        inventory=_inventory(providers=("whoop",)),
    )
    resolver = _resolver(session_factory, client)
    available = await resolver()
    _persist_retained_binding(session_factory, available)
    client.fail_inventory = True

    degraded = await resolver()

    assert degraded.state is OpenWearablesAvailabilityState.DEGRADED
    assert degraded.provider_coverage_digest == (
        available.provider_coverage_digest
    )
    assert degraded.capability_catalog == (
        available.capability_binding("wearable.recovery"),
    )
    assert degraded.providers == ("whoop",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coverage_change",
    ("added", "removed", "malformed"),
)
async def test_changed_or_malformed_coverage_invalidates_lkg(
    session_factory: sessionmaker[Session],
    coverage_change: str,
) -> None:
    class InventoryFailureClient(FakeOpenWearablesClient):
        fail_inventory = False

        async def get_data_summary(self, user_id: str) -> dict:
            if self.fail_inventory:
                raise OWClientError("inventory unavailable")
            return await super().get_data_summary(user_id)

    client = InventoryFailureClient(
        connections=[_active_connection("whoop")],
        data_sources={
            "items": [_data_source("whoop")],
            "total": 1,
        },
        inventory=_inventory(providers=("whoop",)),
    )
    resolver = _resolver(session_factory, client)
    available = await resolver()
    _persist_retained_binding(session_factory, available)

    changed = _coverage()
    if coverage_change == "added":
        changed["timeseries"][0]["metrics"].append(
            {
                "code": "respiratory_rate",
                "unit": "breaths/min",
                "providers": ["whoop"],
            }
        )
    elif coverage_change == "removed":
        changed["health_scores"] = [
            item
            for item in changed["health_scores"]
            if item["code"] != "recovery"
        ]
    else:
        changed["health_scores"] = "invalid"
    client.coverage = changed
    client.fail_inventory = True

    unavailable = await resolver()

    assert unavailable.state is OpenWearablesAvailabilityState.UNAVAILABLE
    assert unavailable.reason_codes == (
        OPEN_WEARABLES_METADATA_UNAVAILABLE,
    )


@pytest.mark.asyncio
async def test_changed_binding_cannot_use_previous_retained_snapshot(
    session_factory: sessionmaker[Session],
) -> None:
    client = FakeOpenWearablesClient(
        connections=[_active_connection("whoop")],
        data_sources={
            "items": [_data_source("whoop")],
            "total": 1,
        },
        inventory=_inventory(providers=("whoop",)),
    )
    resolver = _resolver(session_factory, client)
    available = await resolver()
    assert available.provider_binding_digest is not None
    with session_factory() as session:
        persist_open_wearables_snapshot(
            session,
            normalized_context={
                "date": NOW.date().isoformat(),
                "timezone": "UTC",
                "stress": {"value": 42},
            },
            local_day=NOW.date(),
            timezone="UTC",
            collected_at=NOW,
            now=NOW,
            provider_binding_digest=(
                "sha256:" + "f" * 64
            ),
        )
        session.commit()
    client.error = OWClientError("offline")

    unavailable = await resolver()

    assert unavailable.state is OpenWearablesAvailabilityState.UNAVAILABLE
    assert unavailable.reason_codes == (
        OPEN_WEARABLES_METADATA_UNAVAILABLE,
    )


@pytest.mark.asyncio
async def test_metadata_failure_cancels_and_awaits_sibling_request(
    session_factory: sessionmaker[Session],
) -> None:
    class FailingMetadataClient(FakeOpenWearablesClient):
        def __init__(self) -> None:
            super().__init__()
            self.data_source_started = asyncio.Event()
            self.data_source_cancelled = asyncio.Event()

        async def get_connections(self, user_id: str) -> list[dict]:
            assert user_id == "user-1"
            await self.data_source_started.wait()
            raise OWClientError("connections unavailable")

        async def get_user_data_sources(self, user_id: str) -> dict:
            assert user_id == "user-1"
            self.data_source_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.data_source_cancelled.set()
            raise AssertionError("cancelled metadata request resumed")

    client = FailingMetadataClient()

    snapshot = await _resolver(session_factory, client)()

    assert snapshot.state is OpenWearablesAvailabilityState.UNAVAILABLE
    assert client.data_source_cancelled.is_set()


@pytest.mark.asyncio
async def test_concurrent_resolutions_cannot_restore_lkg_after_disconnect(
    session_factory: sessionmaker[Session],
) -> None:
    class SequencedClient(FakeOpenWearablesClient):
        def __init__(self) -> None:
            super().__init__(
                connections=[_active_connection("whoop")],
                data_sources={
                    "items": [_data_source("whoop")],
                    "total": 1,
                },
                inventory=_inventory(providers=("whoop",)),
            )
            self.calls = 0

        async def get_connections(self, user_id: str) -> list[dict]:
            assert user_id == "user-1"
            self.calls += 1
            if self.calls == 2:
                return []
            return self.connections

    client = SequencedClient()
    resolver = _resolver(session_factory, client)

    first = await resolver()
    disconnected, available = await asyncio.gather(
        resolver(),
        resolver(),
    )

    assert first.state is OpenWearablesAvailabilityState.AVAILABLE
    assert disconnected.state is OpenWearablesAvailabilityState.DISCONNECTED
    assert available.state is OpenWearablesAvailabilityState.AVAILABLE


@pytest.mark.asyncio
async def test_empty_capability_catalog_is_not_available(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=[_active_connection("whoop")],
            data_sources={
                "items": [_data_source("whoop")],
                "total": 1,
            },
            coverage={"providers": ["whoop"]},
            inventory=_inventory(providers=("whoop",)),
        ),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.DISCONNECTED
    assert snapshot.capability_catalog == ()
    assert snapshot.provider_binding_digest is None


def test_source_setting_failure_reason_is_stable_and_blocking() -> None:
    snapshot = OpenWearablesAvailabilitySnapshot(
        state=OpenWearablesAvailabilityState.DISABLED,
        observed_at=NOW,
        reason_codes=(OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE,),
    )

    assert (
        snapshot.blocking_reason_code
        == OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE
    )


def test_provider_binding_change_reason_is_stable_and_blocking() -> None:
    snapshot = OpenWearablesAvailabilitySnapshot(
        state=OpenWearablesAvailabilityState.UNAVAILABLE,
        observed_at=NOW,
        reason_codes=(OPEN_WEARABLES_PROVIDER_BINDING_CHANGED,),
    )

    assert (
        snapshot.blocking_reason_code
        == OPEN_WEARABLES_PROVIDER_BINDING_CHANGED
    )
