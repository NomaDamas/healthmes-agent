from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from healthmes.activity.contracts import (
    ActivityCollectionStatusUpdate,
    ActivityPermissionStatus,
)
from healthmes.activity.repository import (
    APP_INTERVAL_EVENT,
    update_collection_status,
)
from healthmes.decision import (
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextCapability,
    ContextCoverage,
    ContextFreshness,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DecisionCaller,
    DecisionRequest,
    DomainAccessGrant,
    ExecutionScope,
    FreshnessStatus,
    SourceRef,
)
from healthmes.store import Base, WellnessEvent, create_db_engine

NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
DAY_START = datetime(2026, 8, 10, tzinfo=UTC)


class EventProvider:
    def __init__(
        self,
        *,
        domain: str,
        event: WellnessEvent,
    ) -> None:
        self.event = event
        self.metadata = ContextProviderMetadata(
            provider_id=domain,
            domain=domain,
            description="PostgreSQL access boundary provider.",
            capabilities=(
                ContextCapability(
                    capability=f"{domain}.summary",
                    description="PostgreSQL access boundary summary.",
                    granularities=("summary",),
                    query_fields=(
                        "start",
                        "end",
                        "timezone",
                    ),
                    output_fields=("value",),
                    max_lookback_days=1,
                    sensitivity=domain,
                    freshness_expectation="Stored test event.",
                ),
            ),
        )

    async def query(self, session, query, *, now):
        del session
        event = self.event
        raw_window = event.payload.get("window")
        observed_end = (
            datetime.fromisoformat(raw_window["end"])
            if isinstance(raw_window, dict)
            and isinstance(raw_window.get("end"), str)
            else None
        )
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"value": 1},
            source_refs=[
                SourceRef(
                    domain=self.metadata.domain,
                    resource_type=event.event_type,
                    record_id=str(event.id),
                    source_provider=event.source_provider,
                    observed_start=event.observed_at,
                    observed_end=observed_end,
                    schema_version=event.schema_version,
                    derived_by=f"{self.metadata.domain}.test.v1",
                    freshness=FreshnessStatus.CURRENT,
                    coverage=event.coverage,
                    sensitivity=event.sensitivity,
                )
            ],
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
                age_seconds=0,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )


def _event(
    *,
    domain: str,
    event_type: str,
    source_provider: str,
    source_device: str | None,
    expires_at: datetime | None = None,
) -> WellnessEvent:
    observed_at = datetime(2026, 8, 10, 9, tzinfo=UTC)
    return WellnessEvent(
        event_type=event_type,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=observed_at + timedelta(minutes=1),
        timezone="UTC",
        source_provider=source_provider,
        source_device=source_device,
        source_record_id=uuid.uuid4().hex,
        capture_method="test",
        quality_flags={},
        confidence=1,
        coverage=1,
        sensitivity=(
            "activity-identity"
            if domain == "activity"
            else domain
        ),
        consent_scope="personal",
        expires_at=expires_at,
        payload={
            "window": {
                "start": observed_at.isoformat(),
                "end": (
                    observed_at + timedelta(hours=1)
                ).isoformat(),
            }
        },
        derived_from=None,
    )


def _turn(
    provider: EventProvider,
) -> tuple[ContextAccessLayer, DecisionRequest, ContextAccessPolicy]:
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
    )
    request = DecisionRequest(
        question="What context should be considered?",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
    )
    policy = ContextAccessPolicy(
        owner_principal_id="owner",
        grants=(
            DomainAccessGrant(domain=provider.metadata.domain),
        ),
    )
    return layer, request, policy


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
async def test_postgres_enforces_activity_control_and_retention() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        with factory() as session:
            activity = _event(
                domain="activity",
                event_type=APP_INTERVAL_EVENT,
                source_provider="activitywatch",
                source_device="postgres-device",
            )
            nutrition = _event(
                domain="nutrition",
                event_type="nutrition.observation.v1",
                source_provider="healthmes-intake",
                source_device=None,
                expires_at=NOW - timedelta(seconds=1),
            )
            session.add_all((activity, nutrition))
            session.flush()
            update_collection_status(
                session,
                "postgres-device",
                ActivityCollectionStatusUpdate(
                    permission_status=ActivityPermissionStatus.REVOKED,
                    status_observed_at=NOW,
                ),
                now=NOW,
            )

            activity_provider = EventProvider(
                domain="activity",
                event=activity,
            )
            activity_layer, activity_request, activity_policy = _turn(
                activity_provider
            )
            activity_result = await activity_layer.start_turn(
                activity_request,
                policy=activity_policy,
            ).query(
                session,
                ContextQuery(
                    provider_id="activity",
                    capability="activity.summary",
                    start=DAY_START,
                    end=NOW,
                ),
            )

            nutrition_provider = EventProvider(
                domain="nutrition",
                event=nutrition,
            )
            nutrition_layer, nutrition_request, nutrition_policy = _turn(
                nutrition_provider
            )
            nutrition_result = await nutrition_layer.start_turn(
                nutrition_request,
                policy=nutrition_policy,
            ).query(
                session,
                ContextQuery(
                    provider_id="nutrition",
                    capability="nutrition.summary",
                    start=DAY_START,
                    end=NOW,
                ),
            )

            assert activity_result.status is ContextStatus.DENIED
            assert activity_result.limitations == [
                "activity_permission_revoked"
            ]
            assert nutrition_result.status is ContextStatus.DENIED
            assert nutrition_result.limitations == [
                "source_ref_expired"
            ]
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
async def test_postgres_retention_uses_post_provider_wall_clock() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    event_id: uuid.UUID
    with factory() as setup:
        event = _event(
            domain="nutrition",
            event_type="nutrition.observation.v1",
            source_provider="healthmes-intake",
            source_device=None,
        )
        setup.add(event)
        setup.commit()
        event_id = event.id

    class ExpiringProvider(EventProvider):
        async def query(self, session, query, *, now):
            cached = session.get(WellnessEvent, event_id)
            assert cached is not None
            self.event = cached
            result = await super().query(
                session,
                query,
                now=now,
            )
            with factory() as external:
                persisted = external.get(WellnessEvent, event_id)
                assert persisted is not None
                persisted.expires_at = now + timedelta(seconds=1)
                external.commit()
            return result

    clock_values = iter((NOW, NOW + timedelta(seconds=2)))
    provider = ExpiringProvider(
        domain="nutrition",
        event=event,
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: next(clock_values),
    )
    request = DecisionRequest(
        question="What context should be considered?",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
    )
    policy = ContextAccessPolicy(
        owner_principal_id="owner",
        grants=(DomainAccessGrant(domain="nutrition"),),
    )
    try:
        with factory() as primary:
            result = await layer.start_turn(
                request,
                policy=policy,
            ).query(
                primary,
                ContextQuery(
                    provider_id="nutrition",
                    capability="nutrition.summary",
                    start=DAY_START,
                    end=NOW,
                ),
            )

        assert result.status is ContextStatus.DENIED
        assert result.limitations == ["source_ref_expired"]
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()
