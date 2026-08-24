from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings
from healthmes.mcp_server.ow_client import OWClientError
from healthmes.store import InputSourcePolicy
from healthmes.wearables import (
    OPEN_WEARABLES_DISCONNECTED,
    OPEN_WEARABLES_METADATA_DEGRADED,
    OPEN_WEARABLES_METADATA_UNAVAILABLE,
    OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE,
    OPEN_WEARABLES_UNCONFIGURED,
    WEARABLE_INPUT_DISABLED,
    OpenWearablesAvailabilityResolver,
    OpenWearablesAvailabilitySnapshot,
    OpenWearablesAvailabilityState,
    persist_open_wearables_snapshot,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


class FakeOpenWearablesClient:
    def __init__(
        self,
        *,
        users: list[dict] | None = None,
        connections: list[dict] | None = None,
        data_sources: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        self.users = users if users is not None else [{"id": "user-1"}]
        self.connections = connections or []
        self.data_sources = data_sources or {"items": [], "total": 0}
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
@pytest.mark.parametrize(
    ("connections", "data_sources"),
    [
        (
            [{"id": "connection-1", "status": "ACTIVE"}],
            {"items": [], "total": 0},
        ),
        (
            [],
            {
                "items": [
                    {
                        "provider": "apple_health",
                        "user_connection_id": None,
                    }
                ],
                "total": 1,
            },
        ),
    ],
)
async def test_active_connection_or_data_source_is_available(
    session_factory: sessionmaker[Session],
    connections: list[dict],
    data_sources: dict,
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=connections,
            data_sources=data_sources,
        ),
    )()

    assert snapshot.state is OpenWearablesAvailabilityState.AVAILABLE
    assert snapshot.reason_codes == ()
    assert snapshot.exposes_capabilities is True


@pytest.mark.asyncio
async def test_revoked_connection_cannot_be_revived_by_stale_data_source(
    session_factory: sessionmaker[Session],
) -> None:
    snapshot = await _resolver(
        session_factory,
        FakeOpenWearablesClient(
            connections=[
                {
                    "id": "revoked-connection",
                    "status": "DISCONNECTED",
                }
            ],
            data_sources={
                "items": [
                    {
                        "provider": "whoop",
                        "user_connection_id": "revoked-connection",
                    }
                ],
                "total": 1,
            },
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
async def test_metadata_transport_failure_with_valid_retained_data_is_degraded(
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

    assert snapshot.state is OpenWearablesAvailabilityState.DEGRADED
    assert snapshot.reason_codes == (OPEN_WEARABLES_METADATA_DEGRADED,)
    assert snapshot.blocking_reason_code is None
    assert snapshot.exposes_capabilities is True


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
