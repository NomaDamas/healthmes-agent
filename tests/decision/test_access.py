from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import sessionmaker

from healthmes.activity.aggregation import (
    DAY_SUMMARY_EVENT,
    local_day_bounds,
)
from healthmes.activity.contracts import (
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityPermissionStatus,
)
from healthmes.activity.repository import (
    APP_INTERVAL_EVENT,
    create_deletion_tombstone,
    update_collection_config,
    update_collection_status,
)
from healthmes.calendars import creds
from healthmes.calendars.state import (
    InMemorySyncHealthStore,
    SyncCoverageKind,
)
from healthmes.config import Settings
from healthmes.decision import (
    AccessOutcome,
    ActivityContextProvider,
    CalendarContextProvider,
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextCapability,
    ContextCoverage,
    ContextFreshness,
    ContextParameterFormat,
    ContextParameterSpec,
    ContextParameterType,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DecisionBudget,
    DecisionCaller,
    DecisionContextHints,
    DecisionRequest,
    DomainAccessGrant,
    ExecutionScope,
    FreshnessStatus,
    NutritionContextProvider,
    PrivacyLevel,
    ProvenanceSupport,
    SourceRef,
    WearableContextProvider,
)
from healthmes.decision.domain_providers import (
    calendar_aggregate_identity,
)
from healthmes.storage import update_retention_policy
from healthmes.store import (
    Base,
    CalendarEventMirror,
    StorageObject,
    WellnessEvent,
    create_db_engine,
)
from healthmes.store.enums import CalendarSource

NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
DAY_START = datetime(2026, 8, 10, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)


def _parameter_specs(
    parameters: tuple[str, ...],
) -> tuple[ContextParameterSpec, ...]:
    specs = {
        "date": ContextParameterSpec(
            name="date",
            value_type=ContextParameterType.STRING,
            min_length=10,
            max_length=10,
            format=ContextParameterFormat.DATE,
        ),
        "lookback_days": ContextParameterSpec(
            name="lookback_days",
            value_type=ContextParameterType.INTEGER,
            minimum=1,
            maximum=90,
        ),
        "confirmed_only": ContextParameterSpec(
            name="confirmed_only",
            value_type=ContextParameterType.BOOLEAN,
        ),
    }
    return tuple(specs[name] for name in parameters)


class StaticProvider:
    def __init__(
        self,
        *,
        provider_id: str = "mood",
        domain: str = "mood",
        output_fields: tuple[str, ...] = (
            "value",
            "records",
            "app_name",
            "source_text",
            "nested",
        ),
        privacy_levels: tuple[PrivacyLevel, ...] = (
            PrivacyLevel.AGGREGATE,
        ),
        nested_output_fields: tuple[str, ...] = (
            "score",
            "window_title",
        ),
        identity_fields: tuple[str, ...] = (),
        raw_fields: tuple[str, ...] = (),
        limit_output_fields: tuple[str, ...] = (),
        limitation_codes: tuple[str, ...] = (),
        supports_raw: bool = False,
        allows_future: bool = False,
        max_lookback_days: int = 90,
        default_lookback_days: int = 1,
        lookback_parameter: str | None = None,
        lookback_parameter_offset_days: int = 0,
        parameters: tuple[str, ...] = ("date",),
        provenance: ProvenanceSupport = ProvenanceSupport.PARTIAL,
        result_factory: Callable[
            [ContextQuery, datetime],
            Any,
        ]
        | None = None,
    ) -> None:
        self.metadata = ContextProviderMetadata(
            provider_id=provider_id,
            domain=domain,
            description="Test context provider.",
            capabilities=(
                ContextCapability(
                    capability=f"{domain}.summary",
                    description="Test context summary.",
                    granularities=("summary",),
                    query_fields=(
                        "start",
                        "end",
                        "timezone",
                        "fields",
                        "limit",
                    ),
                    output_fields=output_fields,
                    nested_output_fields=nested_output_fields,
                    identity_fields=identity_fields,
                    raw_fields=raw_fields,
                    limit_output_fields=limit_output_fields,
                    limitation_codes=limitation_codes,
                    parameters=parameters,
                    parameter_specs=_parameter_specs(parameters),
                    max_lookback_days=max_lookback_days,
                    default_lookback_days=default_lookback_days,
                    lookback_parameter=lookback_parameter,
                    lookback_parameter_offset_days=(
                        lookback_parameter_offset_days
                    ),
                    privacy_levels=privacy_levels,
                    sensitivity=domain,
                    supports_raw=supports_raw,
                    allows_future=allows_future,
                    provenance=provenance,
                    freshness_expectation="Test snapshot.",
                ),
            ),
        )
        self.queries: list[ContextQuery] = []
        self._result_factory = result_factory

    async def query(self, session, query, *, now):
        del session
        self.queries.append(query)
        if self._result_factory is not None:
            return self._result_factory(query, now)
        return _result(query, now=now)


class BarrierProvider(StaticProvider):
    """Release two provider calls together to exercise turn-level budgets."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._waiting = 0
        self._release = asyncio.Event()

    async def query(self, session, query, *, now):
        del session
        self.queries.append(query)
        self._waiting += 1
        if self._waiting == 2:
            self._release.set()
        await self._release.wait()
        if self._result_factory is not None:
            return self._result_factory(query, now)
        return _result(query, now=now)


def _result(
    query: ContextQuery,
    *,
    now: datetime,
    payload: dict[str, Any] | None = None,
    source_refs: list[SourceRef] | None = None,
    status: ContextStatus = ContextStatus.OK,
    freshness: FreshnessStatus = FreshnessStatus.CURRENT,
    coverage: CoverageStatus = CoverageStatus.COMPLETE,
    limitations: list[str] | None = None,
) -> ContextResult:
    return ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=status,
        payload=payload or {},
        source_refs=source_refs or [],
        freshness=ContextFreshness(
            status=freshness,
            as_of=now
            if freshness
            in {FreshnessStatus.CURRENT, FreshnessStatus.STALE}
            else None,
            age_seconds=0
            if freshness
            in {FreshnessStatus.CURRENT, FreshnessStatus.STALE}
            else None,
        ),
        coverage=ContextCoverage(
            status=coverage,
            ratio=1 if coverage is CoverageStatus.COMPLETE else None,
        ),
        limitations=limitations or [],
    )


def _request(
    *,
    principal_id: str = "owner",
    authenticated: bool = True,
    execution_scope: ExecutionScope = ExecutionScope.LOCAL,
    timezone: str = "UTC",
    privacy: PrivacyLevel = PrivacyLevel.AGGREGATE,
    budget: DecisionBudget | None = None,
    related_record_ids: dict[str, str] | None = None,
) -> DecisionRequest:
    return DecisionRequest(
        question="What context should be considered?",
        requested_at=NOW,
        timezone=timezone,
        caller=DecisionCaller(
            principal_id=principal_id,
            authenticated=authenticated,
            execution_scope=execution_scope,
        ),
        requested_privacy_level=privacy,
        budget=budget or DecisionBudget(),
        hints=DecisionContextHints(
            related_record_ids=related_record_ids or {},
        ),
    )


def _policy(
    *,
    owner: str = "owner",
    domain: str = "mood",
    enabled: bool = True,
    privacy: PrivacyLevel = PrivacyLevel.AGGREGATE,
    execution_scopes: tuple[ExecutionScope, ...] = (
        ExecutionScope.LOCAL,
        ExecutionScope.HOSTED,
    ),
    consent_scopes: tuple[str, ...] = ("personal",),
    allow_hosted_raw: bool = False,
    **limits,
) -> ContextAccessPolicy:
    return ContextAccessPolicy(
        owner_principal_id=owner,
        grants=(
            DomainAccessGrant(
                domain=domain,
                enabled=enabled,
                max_privacy_level=privacy,
                execution_scopes=execution_scopes,
                consent_scopes=consent_scopes,
                allow_hosted_raw=allow_hosted_raw,
            ),
        ),
        **limits,
    )


def _query(
    *,
    provider_id: str = "mood",
    domain: str = "mood",
    timezone: str = "UTC",
    privacy: PrivacyLevel = PrivacyLevel.AGGREGATE,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    date_value: str | None = None,
    purpose: str | None = None,
) -> ContextQuery:
    return ContextQuery(
        provider_id=provider_id,
        capability=f"{domain}.summary",
        timezone=timezone,
        privacy_level=privacy,
        start=start,
        end=end,
        limit=limit,
        parameters={"date": date_value} if date_value is not None else {},
        purpose=purpose,
    )


def _event(
    *,
    domain: str,
    event_type: str | None = None,
    source_provider: str = "test-provider",
    source_record_id: str | None = None,
    source_device: str | None = "device-a",
    observed_at: datetime = datetime(2026, 8, 10, 9, tzinfo=UTC),
    observed_end: datetime | None = None,
    recorded_at: datetime = datetime(2026, 8, 10, 9, 5, tzinfo=UTC),
    expires_at: datetime | None = None,
    consent_scope: str = "personal",
    sensitivity: str | None = None,
    coverage: float | None = 1.0,
    raw_object_id: uuid.UUID | None = None,
) -> WellnessEvent:
    payload: dict[str, Any] = {}
    if observed_end is not None:
        payload["window"] = {
            "start": observed_at.isoformat(),
            "end": observed_end.isoformat(),
        }
    return WellnessEvent(
        event_type=event_type or f"{domain}.observation.v1",
        schema_version=1,
        observed_at=observed_at,
        recorded_at=recorded_at,
        timezone="UTC",
        source_provider=source_provider,
        source_device=source_device,
        source_record_id=source_record_id or uuid.uuid4().hex,
        capture_method="test",
        quality_flags={},
        confidence=1.0,
        coverage=coverage,
        sensitivity=sensitivity or domain,
        consent_scope=consent_scope,
        expires_at=expires_at,
        payload=payload,
        raw_object_id=raw_object_id,
        derived_from=None,
    )


def _source_ref(
    event: WellnessEvent,
    *,
    domain: str,
    source_provider: str | None = None,
    observed_start: datetime | None = None,
    observed_end: datetime | None | object = ...,
    sensitivity: str | None = None,
) -> SourceRef:
    if observed_end is ...:
        raw_window = event.payload.get("window")
        raw_end = (
            raw_window.get("end")
            if isinstance(raw_window, dict)
            else None
        )
        resolved_end = (
            datetime.fromisoformat(raw_end)
            if isinstance(raw_end, str)
            else None
        )
    else:
        resolved_end = observed_end
    return SourceRef(
        domain=domain,
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=source_provider or event.source_provider,
        observed_start=observed_start or event.observed_at,
        observed_end=resolved_end,
        schema_version=event.schema_version,
        derived_by=f"{domain}.test.v1",
        freshness=FreshnessStatus.CURRENT,
        coverage=event.coverage,
        sensitivity=sensitivity or event.sensitivity,
    )


def _turn(
    provider: StaticProvider,
    *,
    request: DecisionRequest | None = None,
    policy: ContextAccessPolicy | None = None,
) -> tuple[ContextAccessLayer, Any]:
    registry = ContextProviderRegistry((provider,))
    layer = ContextAccessLayer(registry, clock=lambda: NOW)
    current_request = request or _request()
    return layer, layer.start_turn(
        current_request,
        policy=policy or _policy(domain=provider.metadata.domain),
    )


@pytest.mark.parametrize(
    ("decision_request", "policy", "reason"),
    (
        (
            _request(authenticated=False),
            _policy(),
            "caller_not_authenticated",
        ),
        (
            _request(principal_id="other"),
            _policy(owner="owner"),
            "caller_not_policy_owner",
        ),
    ),
)
async def test_authentication_and_owner_binding_fail_closed(
    session,
    decision_request,
    policy,
    reason,
):
    provider = StaticProvider()
    _, turn = _turn(
        provider,
        request=decision_request,
        policy=policy,
    )

    result = await turn.query(session, _query())

    assert result.status is ContextStatus.DENIED
    assert result.limitations == [reason]
    assert provider.queries == []
    assert turn.trace[0].outcome is AccessOutcome.DENIED


async def test_effective_duplicate_rejection_is_opt_in(session):
    provider = StaticProvider()
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(_request(), policy=_policy())

    first = await turn.query(session, _query(limit=999))
    second = await turn.query(session, _query(limit=1_000))

    assert first.status is ContextStatus.PARTIAL
    assert second.status is ContextStatus.PARTIAL
    assert first.limitations == ["query_limit_trimmed"]
    assert second.limitations == ["query_limit_trimmed"]
    assert [query.limit for query in provider.queries] == [250, 250]


async def test_effective_duplicate_uses_one_turn_normalization_time(session):
    provider = StaticProvider()
    current = NOW

    def ticking_clock():
        nonlocal current
        current += timedelta(microseconds=100)
        return current

    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=ticking_clock,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(),
        reject_duplicate_effective_queries=True,
    )

    first = await turn.query(session, _query(limit=999))
    second = await turn.query(session, _query(limit=1_000))

    assert first.status is ContextStatus.PARTIAL
    assert second.status is ContextStatus.DENIED
    assert second.limitations == ["duplicate_tool_call"]
    assert len(provider.queries) == 1


async def test_domain_consent_and_provider_enablement_fail_closed(session):
    provider = StaticProvider()
    registry = ContextProviderRegistry((provider,))
    layer = ContextAccessLayer(registry, clock=lambda: NOW)
    request = _request()
    missing_grant = ContextAccessPolicy(
        owner_principal_id="owner",
        grants=(),
    )

    denied = await layer.start_turn(
        request,
        policy=missing_grant,
    ).query(session, _query())
    registry.set_enabled("mood", enabled=False)
    disabled = await layer.start_turn(
        request,
        policy=_policy(),
    ).query(session, _query())

    assert denied.limitations == ["domain_consent_denied"]
    assert disabled.limitations == ["provider_disabled"]
    assert provider.queries == []


async def test_execution_scope_is_enforced_without_affecting_local_access(
    session,
):
    provider = StaticProvider()
    local_only = _policy(
        execution_scopes=(ExecutionScope.LOCAL,),
    )
    _, local_turn = _turn(
        provider,
        request=_request(execution_scope=ExecutionScope.LOCAL),
        policy=local_only,
    )
    _, hosted_turn = _turn(
        provider,
        request=_request(execution_scope=ExecutionScope.HOSTED),
        policy=local_only,
    )

    local = await local_turn.query(session, _query())
    hosted = await hosted_turn.query(session, _query())

    assert local.status is ContextStatus.OK
    assert hosted.status is ContextStatus.DENIED
    assert hosted.limitations == ["execution_scope_denied"]


@pytest.mark.parametrize(
    ("privacy", "expected_keys"),
    (
        (PrivacyLevel.AGGREGATE, {"value", "nested"}),
        (
            PrivacyLevel.IDENTITY,
            {"value", "app_name", "nested"},
        ),
        (
            PrivacyLevel.SCOPED_RAW,
            {
                "value",
                "app_name",
                "nested",
            },
        ),
    ),
)
async def test_privacy_levels_redact_without_implicit_escalation(
    session,
    privacy,
    expected_keys,
):
    raw = StorageObject(
        data_class="mood_raw_capture",
        relative_path=f"mood/{privacy.value}.json",
        content_type="application/json",
        size_bytes=128,
        sha256="1" * 64,
        expires_at=None,
        safe_to_purge=True,
        purged_at=None,
    )
    session.add(raw)
    session.flush()
    event = _event(
        domain="mood",
        raw_object_id=raw.id,
    )
    session.add(event)
    session.flush()

    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={
                "value": 7,
                "app_name": "Private App",
                "source_text": "private transcript",
                "nested": {
                    "window_title": "Secret Project",
                    "score": 1,
                },
            },
            source_refs=[_source_ref(event, domain="mood")],
        )

    provider = StaticProvider(
        privacy_levels=(
            PrivacyLevel.AGGREGATE,
            PrivacyLevel.IDENTITY,
            PrivacyLevel.SCOPED_RAW,
        ),
        supports_raw=True,
        result_factory=result_factory,
    )
    request = _request(
        privacy=privacy,
        related_record_ids={"capture": event.source_record_id},
    )
    _, turn = _turn(
        provider,
        request=request,
        policy=_policy(privacy=PrivacyLevel.SCOPED_RAW),
    )

    result = await turn.query(
        session,
        _query(
            privacy=privacy,
            purpose="Answer the current question"
            if privacy is PrivacyLevel.SCOPED_RAW
            else None,
        ),
    )

    assert set(result.payload) == expected_keys
    assert result.payload["nested"] == (
        {"window_title": "Secret Project", "score": 1}
        if privacy
        in {PrivacyLevel.IDENTITY, PrivacyLevel.SCOPED_RAW}
        else {"score": 1}
    )
    assert len(result.raw_sources) == (
        1 if privacy is PrivacyLevel.SCOPED_RAW else 0
    )
    if result.raw_sources:
        assert result.raw_sources[0].storage_object_id == raw.id
    assert "private transcript" not in result.model_dump_json()
    assert "private transcript" not in turn.trace[0].model_dump_json()
    assert "Secret Project" not in turn.trace[0].model_dump_json()


async def test_query_cannot_raise_privacy_above_request(session):
    provider = StaticProvider(
        privacy_levels=(
            PrivacyLevel.AGGREGATE,
            PrivacyLevel.IDENTITY,
        )
    )
    _, turn = _turn(
        provider,
        request=_request(privacy=PrivacyLevel.AGGREGATE),
        policy=_policy(privacy=PrivacyLevel.IDENTITY),
    )

    result = await turn.query(
        session,
        _query(privacy=PrivacyLevel.IDENTITY),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["privacy_implicit_escalation_denied"]


async def test_hosted_raw_requires_explicit_hosted_raw_consent(session):
    raw = StorageObject(
        data_class="mood_raw_capture",
        relative_path="mood/hosted.json",
        content_type="application/json",
        size_bytes=128,
        sha256="3" * 64,
        expires_at=None,
        safe_to_purge=True,
        purged_at=None,
    )
    session.add(raw)
    session.flush()
    event = _event(domain="mood", raw_object_id=raw.id)
    session.add(event)
    session.flush()
    provider = StaticProvider(
        privacy_levels=(PrivacyLevel.SCOPED_RAW,),
        supports_raw=True,
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"source_text": "private hosted raw"},
            source_refs=[_source_ref(event, domain="mood")],
        ),
    )
    request = _request(
        execution_scope=ExecutionScope.HOSTED,
        privacy=PrivacyLevel.SCOPED_RAW,
        related_record_ids={"capture": event.source_record_id},
    )
    _, denied_turn = _turn(
        provider,
        request=request,
        policy=_policy(privacy=PrivacyLevel.SCOPED_RAW),
    )
    _, allowed_turn = _turn(
        provider,
        request=request,
        policy=_policy(
            privacy=PrivacyLevel.SCOPED_RAW,
            allow_hosted_raw=True,
        ),
    )

    denied = await denied_turn.query(
        session,
        _query(
            privacy=PrivacyLevel.SCOPED_RAW,
            purpose="Analyze one explicitly selected capture",
        ),
    )
    allowed = await allowed_turn.query(
        session,
        _query(
            privacy=PrivacyLevel.SCOPED_RAW,
            purpose="Analyze one explicitly selected capture",
        ),
    )

    assert denied.limitations == ["scoped_raw_access_denied"]
    assert allowed.status is ContextStatus.PARTIAL
    assert allowed.raw_sources[0].storage_object_id == raw.id
    assert "private hosted raw" not in allowed.model_dump_json()


def _activity_provider_for(
    event: WellnessEvent,
) -> StaticProvider:
    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"value": 45},
            source_refs=[_source_ref(event, domain="activity")],
        )

    return StaticProvider(
        provider_id="activity",
        domain="activity",
        result_factory=result_factory,
    )


@pytest.mark.parametrize(
    ("boundary", "reason"),
    (
        ("disabled", "activity_collection_disabled"),
        ("denied", "activity_permission_denied"),
        ("revoked", "activity_permission_revoked"),
        ("unavailable", "activity_permission_unavailable"),
        ("paused", "activity_collection_paused"),
    ),
)
async def test_activity_collection_privacy_boundaries_block_matching_data(
    session,
    boundary,
    reason,
):
    event = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="device-a",
        observed_end=datetime(2026, 8, 10, 10, tzinfo=UTC),
        sensitivity="activity-identity",
    )
    session.add(event)
    session.flush()
    if boundary == "disabled":
        update_collection_config(
            session,
            "device-a",
            ActivityCollectionUpdate(enabled=False),
            now=NOW,
        )
    elif boundary == "paused":
        update_collection_config(
            session,
            "device-a",
            ActivityCollectionUpdate(
                paused_until=NOW + timedelta(hours=1)
            ),
            now=NOW,
        )
    else:
        update_collection_status(
            session,
            "device-a",
            ActivityCollectionStatusUpdate(
                permission_status=ActivityPermissionStatus(boundary),
                status_observed_at=NOW,
            ),
            now=NOW,
        )
    provider = _activity_provider_for(event)
    _, turn = _turn(
        provider,
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="activity",
            domain="activity",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == [reason]
    assert provider.queries == []


async def test_unrelated_blocked_activity_device_does_not_block_query(
    session,
):
    selected = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="selected-device",
        observed_end=datetime(2026, 8, 10, 10, tzinfo=UTC),
        sensitivity="activity-identity",
    )
    unrelated = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="unrelated-device",
        observed_at=datetime(2026, 8, 7, 9, tzinfo=UTC),
        observed_end=datetime(2026, 8, 7, 10, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 7, 10, tzinfo=UTC),
        sensitivity="activity-identity",
    )
    session.add_all((selected, unrelated))
    session.flush()
    update_collection_config(
        session,
        "unrelated-device",
        ActivityCollectionUpdate(enabled=False),
        now=NOW,
    )
    provider = _activity_provider_for(selected)
    _, turn = _turn(
        provider,
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="activity",
            domain="activity",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.OK
    assert result.source_refs[0].record_id == str(selected.id)


async def test_activity_interval_starting_before_query_is_retained_when_overlapping(
    session,
):
    interval_start = datetime(2026, 8, 10, 8, 30, tzinfo=UTC)
    interval_end = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    event = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="overlap-device",
        observed_at=interval_start,
        recorded_at=interval_end,
        sensitivity="activity-identity",
    )
    event.payload = {
        "kind": "app_interval",
        "platform": "macos",
        "capability": "detailed",
        "start_at": interval_start.isoformat(),
        "end_at": interval_end.isoformat(),
        "state": "active",
        "app_id": "editor",
        "launches": 1,
        "category": "productivity",
    }
    session.add(event)
    session.flush()
    layer = ContextAccessLayer(
        ContextProviderRegistry((ActivityContextProvider(),)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="activity",
            capability="activity.timeline",
            start=datetime(2026, 8, 10, 9, tzinfo=UTC),
            end=datetime(2026, 8, 10, 10, tzinfo=UTC),
            granularity="window",
        ),
    )

    assert result.status in {ContextStatus.OK, ContextStatus.PARTIAL}
    assert len(result.source_refs) == 1
    assert result.source_refs[0].record_id == str(event.id)
    assert result.source_refs[0].observed_start == interval_start
    assert result.source_refs[0].observed_end == interval_end
    assert "source_ref_outside_query" not in result.limitations


async def test_expired_activity_device_does_not_block_default_query(
    session,
):
    expired = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="expired-device",
        observed_end=datetime(2026, 8, 10, 10, tzinfo=UTC),
        expires_at=NOW - timedelta(seconds=1),
        sensitivity="activity-identity",
    )
    session.add(expired)
    session.flush()
    update_collection_config(
        session,
        "expired-device",
        ActivityCollectionUpdate(enabled=False),
        now=NOW,
    )
    provider = StaticProvider(
        provider_id="activity",
        domain="activity",
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        _query(provider_id="activity", domain="activity"),
    )

    assert result.status is ContextStatus.OK
    assert provider.queries


async def test_activity_permission_is_rechecked_after_provider_execution(
    session,
):
    event = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="revoked-during-query",
        observed_end=datetime(2026, 8, 10, 10, tzinfo=UTC),
        sensitivity="activity-identity",
    )
    session.add(event)
    session.flush()

    class RevokingProvider(StaticProvider):
        async def query(self, session, query, *, now):
            self.queries.append(query)
            update_collection_status(
                session,
                "revoked-during-query",
                ActivityCollectionStatusUpdate(
                    permission_status=ActivityPermissionStatus.REVOKED,
                    status_observed_at=now,
                ),
                now=now,
            )
            return _result(query, now=now)

    provider = RevokingProvider(
        provider_id="activity",
        domain="activity",
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="activity",
            domain="activity",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["activity_permission_revoked"]


async def test_retention_is_rechecked_after_provider_execution(session):
    event = _event(
        domain="nutrition",
        expires_at=NOW + timedelta(seconds=1),
    )
    session.add(event)
    session.flush()
    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 10},
            source_refs=[_source_ref(event, domain="nutrition")],
        ),
    )
    clock_values = iter((NOW, NOW + timedelta(seconds=2)))
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: next(clock_values),
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["source_ref_expired"]
    assert turn.trace[0].occurred_at == NOW + timedelta(seconds=2)


async def test_postflight_refreshes_cross_session_sqlite_retention(
    tmp_path,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path}/decision-access.db"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event_id: uuid.UUID
    with factory() as setup:
        event = _event(domain="nutrition")
        setup.add(event)
        setup.commit()
        event_id = event.id

    class ExpiringProvider(StaticProvider):
        async def query(self, session, query, *, now):
            self.queries.append(query)
            cached = session.get(WellnessEvent, event_id)
            assert cached is not None
            source_ref = _source_ref(
                cached,
                domain="nutrition",
                observed_start=cached.observed_at.replace(tzinfo=UTC),
            )
            with factory() as external:
                persisted = external.get(WellnessEvent, event_id)
                assert persisted is not None
                persisted.expires_at = now + timedelta(seconds=1)
                external.commit()
            return _result(
                query,
                now=now,
                payload={"value": 10},
                source_refs=[source_ref],
            )

    provider = ExpiringProvider(
        provider_id="nutrition",
        domain="nutrition",
    )
    clock_values = iter((NOW, NOW + timedelta(seconds=2)))
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: next(clock_values),
    )
    try:
        with factory() as primary:
            turn = layer.start_turn(
                _request(),
                policy=_policy(domain="nutrition"),
            )
            result = await turn.query(
                primary,
                _query(
                    provider_id="nutrition",
                    domain="nutrition",
                    start=DAY_START,
                    end=NOW,
                ),
            )

        assert result.status is ContextStatus.DENIED
        assert result.limitations == ["source_ref_expired"]
    finally:
        engine.dispose()


async def test_postflight_rejects_cross_session_calendar_all_day_change(
    tmp_path,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path}/decision-calendar-access.db"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event_id: uuid.UUID
    with factory() as setup:
        row = CalendarEventMirror(
            external_id="calendar-cross-session-all-day",
            calendar_source=CalendarSource.GOOGLE,
            summary="Private title",
            start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
            end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
            is_all_day=False,
            created_at=NOW,
            updated_at=NOW,
        )
        setup.add(row)
        setup.commit()
        event_id = row.id

    class AllDayChangingProvider(CalendarContextProvider):
        cached: CalendarEventMirror | None = None

        async def query(self, session, query, *, now):
            self.cached = session.get(CalendarEventMirror, event_id)
            assert self.cached is not None
            result = await super().query(session, query, now=now)
            with factory() as external:
                persisted = external.get(CalendarEventMirror, event_id)
                assert persisted is not None
                persisted.is_all_day = True
                external.commit()
            return result

    provider = AllDayChangingProvider(
        sources=(CalendarSource.GOOGLE,),
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
    )
    try:
        with factory() as primary:
            turn = layer.start_turn(
                _request(),
                policy=_policy(domain="calendar"),
            )
            result = await turn.query(
                primary,
                ContextQuery(
                    provider_id="calendar",
                    capability="calendar.busy-intervals",
                    start=datetime(2026, 8, 10, 8, tzinfo=UTC),
                    end=datetime(2026, 8, 10, 12, tzinfo=UTC),
                    granularity="window",
                ),
            )

            assert result.status is ContextStatus.DENIED
            assert result.limitations == ["source_ref_content_changed"]
            assert provider.cached is not None
            assert provider.cached.is_all_day is False
    finally:
        engine.dispose()


async def test_provider_enablement_is_rechecked_after_execution(session):
    registry: ContextProviderRegistry

    def disable_during_query(query, now):
        registry.set_enabled("mood", enabled=False)
        return _result(
            query,
            now=now,
            payload={"value": 7},
        )

    provider = StaticProvider(result_factory=disable_during_query)
    registry = ContextProviderRegistry((provider,))
    layer = ContextAccessLayer(registry, clock=lambda: NOW)
    turn = layer.start_turn(_request(), policy=_policy())

    result = await turn.query(session, _query())

    assert result.status is ContextStatus.DENIED
    assert result.payload == {}
    assert result.limitations == ["provider_access_changed"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("expired", "source_ref_expired"),
        ("missing", "source_ref_record_missing"),
        ("identity", "source_ref_identity_mismatch"),
        ("consent", "source_consent_scope_denied"),
    ),
)
async def test_invalid_local_source_references_are_denied(
    session,
    mutation,
    reason,
):
    event = _event(
        domain="nutrition",
        expires_at=NOW - timedelta(seconds=1)
        if mutation == "expired"
        else None,
        consent_scope="research" if mutation == "consent" else "personal",
    )
    session.add(event)
    session.flush()
    if mutation == "missing":
        ref = SourceRef(
            domain="nutrition",
            resource_type=event.event_type,
            record_id=str(uuid.uuid4()),
            source_provider=event.source_provider,
            observed_start=event.observed_at,
            schema_version=1,
            derived_by="nutrition.test.v1",
            freshness=FreshnessStatus.CURRENT,
            coverage=event.coverage,
            sensitivity=event.sensitivity,
        )
    elif mutation == "identity":
        ref = _source_ref(
            event,
            domain="nutrition",
            source_provider="forged-provider",
        )
    else:
        ref = _source_ref(event, domain="nutrition")

    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"value": 10},
            source_refs=[ref],
        )

    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=result_factory,
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == [reason]


async def test_forged_observation_window_and_coverage_are_denied(session):
    event = _event(
        domain="nutrition",
        observed_end=datetime(2026, 8, 10, 10, tzinfo=UTC),
    )
    session.add(event)
    session.flush()
    forged_window = _source_ref(
        event,
        domain="nutrition",
        observed_end=datetime(2026, 8, 10, 11, tzinfo=UTC),
    )
    forged_coverage = forged_window.model_copy(update={"coverage": 0.5})
    refs = iter((forged_window, forged_coverage))

    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"value": 10},
            source_refs=[next(refs)],
        )

    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=result_factory,
    )
    _, first_turn = _turn(
        provider,
        policy=_policy(domain="nutrition"),
    )
    _, second_turn = _turn(
        provider,
        policy=_policy(domain="nutrition"),
    )
    query = _query(
        provider_id="nutrition",
        domain="nutrition",
        start=DAY_START,
        end=NOW,
    )

    window_result = await first_turn.query(session, query)
    coverage_result = await second_turn.query(
        session,
        query.model_copy(update={"query_id": uuid.uuid4()}),
    )

    assert window_result.limitations == ["source_ref_identity_mismatch"]
    assert coverage_result.limitations == ["source_ref_identity_mismatch"]


async def test_activity_tombstone_only_blocks_affected_device(session):
    blocked = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="blocked-device",
        observed_end=datetime(2026, 8, 10, 10, tzinfo=UTC),
        sensitivity="activity-identity",
    )
    unaffected = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="other-device",
        observed_end=datetime(2026, 8, 10, 10, tzinfo=UTC),
        source_record_id="other-record",
        sensitivity="activity-identity",
    )
    session.add_all((blocked, unaffected))
    session.flush()
    create_deletion_tombstone(
        session,
        device_id="blocked-device",
        start=DAY_START,
        end=NOW,
        now=NOW,
    )
    blocked_provider = _activity_provider_for(blocked)
    unaffected_provider = _activity_provider_for(unaffected)
    _, blocked_turn = _turn(
        blocked_provider,
        policy=_policy(domain="activity"),
    )
    _, unaffected_turn = _turn(
        unaffected_provider,
        policy=_policy(domain="activity"),
    )
    query = _query(
        provider_id="activity",
        domain="activity",
        start=DAY_START,
        end=NOW,
    )

    blocked_result = await blocked_turn.query(session, query)
    unaffected_result = await unaffected_turn.query(
        session,
        query.model_copy(update={"query_id": uuid.uuid4()}),
    )

    assert blocked_result.limitations == ["source_ref_tombstoned"]
    assert unaffected_result.status is ContextStatus.OK


async def test_scoped_raw_requires_live_storage_object(session):
    raw = StorageObject(
        data_class="nutrition_raw_capture",
        relative_path="nutrition/purged.json",
        content_type="application/json",
        size_bytes=128,
        sha256="0" * 64,
        expires_at=None,
        safe_to_purge=True,
        purged_at=NOW,
    )
    session.add(raw)
    session.flush()
    event = _event(
        domain="nutrition",
        raw_object_id=raw.id,
    )
    session.add(event)
    session.flush()

    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"source_text": "selected raw capture"},
            source_refs=[_source_ref(event, domain="nutrition")],
        )

    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        output_fields=("source_text",),
        privacy_levels=(PrivacyLevel.SCOPED_RAW,),
        supports_raw=True,
        result_factory=result_factory,
    )
    _, turn = _turn(
        provider,
        request=_request(
            privacy=PrivacyLevel.SCOPED_RAW,
            related_record_ids={"capture": event.source_record_id},
        ),
        policy=_policy(
            domain="nutrition",
            privacy=PrivacyLevel.SCOPED_RAW,
        ),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            privacy=PrivacyLevel.SCOPED_RAW,
            start=DAY_START,
            end=NOW,
            purpose="Read one selected retained capture",
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["raw_source_unavailable"]


async def test_scoped_raw_requires_an_explicit_selected_record(session):
    provider = StaticProvider(
        privacy_levels=(PrivacyLevel.SCOPED_RAW,),
        supports_raw=True,
    )
    _, turn = _turn(
        provider,
        request=_request(privacy=PrivacyLevel.SCOPED_RAW),
        policy=_policy(privacy=PrivacyLevel.SCOPED_RAW),
    )

    result = await turn.query(
        session,
        _query(
            privacy=PrivacyLevel.SCOPED_RAW,
            purpose="Inspect one capture",
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["scoped_raw_selection_required"]
    assert provider.queries == []


async def test_scoped_raw_ref_must_match_the_selected_record(session):
    raw = StorageObject(
        data_class="nutrition_raw_capture",
        relative_path="nutrition/live.json",
        content_type="application/json",
        size_bytes=128,
        sha256="2" * 64,
        expires_at=None,
        safe_to_purge=True,
        purged_at=None,
    )
    session.add(raw)
    session.flush()
    event = _event(domain="nutrition", raw_object_id=raw.id)
    session.add(event)
    session.flush()
    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        output_fields=("source_text",),
        privacy_levels=(PrivacyLevel.SCOPED_RAW,),
        supports_raw=True,
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"source_text": "selected raw capture"},
            source_refs=[_source_ref(event, domain="nutrition")],
        ),
    )
    _, turn = _turn(
        provider,
        request=_request(
            privacy=PrivacyLevel.SCOPED_RAW,
            related_record_ids={"capture": "different-record"},
        ),
        policy=_policy(
            domain="nutrition",
            privacy=PrivacyLevel.SCOPED_RAW,
        ),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            privacy=PrivacyLevel.SCOPED_RAW,
            start=DAY_START,
            end=NOW,
            purpose="Inspect one capture",
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["raw_source_not_selected"]


async def test_scoped_raw_never_trusts_provider_supplied_raw_content(session):
    selected_raw = StorageObject(
        data_class="nutrition_raw_capture",
        relative_path="nutrition/selected.json",
        content_type="application/json",
        size_bytes=64,
        sha256="4" * 64,
        expires_at=None,
        safe_to_purge=True,
        purged_at=None,
    )
    other_raw = StorageObject(
        data_class="nutrition_raw_capture",
        relative_path="nutrition/other.json",
        content_type="application/json",
        size_bytes=64,
        sha256="5" * 64,
        expires_at=None,
        safe_to_purge=True,
        purged_at=None,
    )
    session.add_all((selected_raw, other_raw))
    session.flush()
    selected = _event(domain="nutrition", raw_object_id=selected_raw.id)
    other = _event(domain="nutrition", raw_object_id=other_raw.id)
    session.add_all((selected, other))
    session.flush()
    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        output_fields=("source_text",),
        privacy_levels=(PrivacyLevel.SCOPED_RAW,),
        supports_raw=True,
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"source_text": "raw content from the other record"},
            source_refs=[_source_ref(selected, domain="nutrition")],
        ),
    )
    _, turn = _turn(
        provider,
        request=_request(
            privacy=PrivacyLevel.SCOPED_RAW,
            related_record_ids={"capture": selected.source_record_id},
        ),
        policy=_policy(
            domain="nutrition",
            privacy=PrivacyLevel.SCOPED_RAW,
        ),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            privacy=PrivacyLevel.SCOPED_RAW,
            start=DAY_START,
            end=NOW,
            purpose="Inspect one selected capture",
        ),
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload == {}
    assert result.raw_sources[0].storage_object_id == selected_raw.id
    assert str(other_raw.id) not in result.model_dump_json()
    assert "raw content from the other record" not in result.model_dump_json()


async def test_capability_nested_schema_blocks_undeclared_private_fields(
    session,
):
    provider = StaticProvider(
        output_fields=("records",),
        nested_output_fields=("safe_metric", "question"),
        identity_fields=("question",),
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={
                "records": [
                    {
                        "safe_metric": 7,
                        "question": "private stored question",
                        "secret_condition": "must not cross",
                    }
                ]
            },
        ),
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    assert result.payload == {"records": [{"safe_metric": 7}]}
    serialized = result.model_dump_json()
    audit = turn.trace[0].model_dump_json()
    assert "private stored question" not in serialized
    assert "must not cross" not in serialized
    assert "private stored question" not in audit
    assert "must not cross" not in audit


@pytest.mark.parametrize(
    ("days_before", "expected_status"),
    (
        (2, ContextStatus.PARTIAL),
        (3, ContextStatus.DENIED),
    ),
)
async def test_open_wearables_provenance_accepts_only_bounded_lookback(
    session,
    days_before,
    expected_status,
):
    selected_day = DAY_START - timedelta(days=1)
    observed = selected_day - timedelta(days=days_before)
    ref = SourceRef(
        domain="wearable",
        resource_type="sleep_summary",
        record_id=f"sleep-{days_before}",
        source_provider="open-wearables",
        observed_start=observed,
        observed_end=observed + timedelta(days=1),
        schema_version=1,
        derived_by="open-wearables.daily-readiness.v1",
        freshness=FreshnessStatus.CURRENT,
        sensitivity="wearable",
    )

    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"value": 80},
            source_refs=[ref],
        )

    provider = StaticProvider(
        provider_id="wearable",
        domain="wearable",
        result_factory=result_factory,
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="wearable"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="wearable",
            domain="wearable",
            date_value="2026-08-09",
        ),
    )

    assert result.status is expected_status
    if days_before == 2:
        assert (
            "external_source_retention_unverified"
            in result.limitations
        )
    else:
        assert result.limitations == ["source_ref_outside_query"]


async def test_external_provenance_can_be_disabled_by_policy(session):
    ref = SourceRef(
        domain="wearable",
        resource_type="health_score",
        record_id="score-1",
        source_provider="open-wearables",
        observed_start=datetime(2026, 8, 10, 8, tzinfo=UTC),
        schema_version=1,
        derived_by="open-wearables.daily-readiness.v1",
        freshness=FreshnessStatus.CURRENT,
        sensitivity="wearable",
    )

    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"value": 80},
            source_refs=[ref],
        )

    provider = StaticProvider(
        provider_id="wearable",
        domain="wearable",
        result_factory=result_factory,
    )
    policy = _policy(domain="wearable").model_copy(
        update={"allow_external_provenance": False}
    )
    _, turn = _turn(provider, policy=policy)

    result = await turn.query(
        session,
        _query(
            provider_id="wearable",
            domain="wearable",
            date_value="2026-08-09",
        ),
    )

    assert result.limitations == ["external_source_provenance_denied"]


async def test_wearable_provider_returns_attested_local_snapshot(
    session,
):
    async def reader(day):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "stress": {
                "status": "ok",
                "value": 42,
                "recorded_at": "2026-08-10T08:00:00+00:00",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": "score-1",
                    "source_provider": "open-wearables",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-10T08:00:00+00:00",
                    "schema_version": 1,
                    "derived_by": (
                        "open-wearables.daily-readiness.v1"
                    ),
                }
            ],
            "freshness": {
                "recorded_at": "2026-08-10T08:00:00+00:00",
                "status": "derived_from_readiness_blocks",
            },
            "coverage": {
                "status": "readiness_blocks",
                "ratio": 1.0,
            },
            "limitations": [],
        }

    factory = sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
    )
    provider = WearableContextProvider(
        reader,
        snapshot_session_factory=factory,
    )
    registry = ContextProviderRegistry((provider,))
    layer = ContextAccessLayer(registry, clock=lambda: NOW)
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="wearable"),
    )
    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.stress",
        timezone="UTC",
        parameters={"date": "2026-08-10"},
    )

    result = await turn.query(session, query)

    assert result.payload["stress"]["value"] == 42
    assert len(result.source_refs) == 1
    ref = result.source_refs[0]
    assert ref.source_provider == "healthmes-open-wearables-mirror"
    assert ref.record_id != "score-1"
    assert ref.content_digest is not None
    assert ref.collected_at == NOW
    assert result.collected_at == NOW
    validated, limitations = turn.revalidate_source_ref(
        session,
        query,
        ref,
        context_source_refs=result.source_refs,
        now=NOW,
    )
    assert validated == ref
    assert limitations == ("future_range_trimmed",)


async def test_wearable_source_ref_rejects_expired_upstream_calendar_row(
    session,
):
    update_retention_policy(
        session,
        "calendar_mirror",
        "1d",
        now=NOW,
    )
    row = CalendarEventMirror(
        external_id="expired-upstream-actual-sleep",
        calendar_source=CalendarSource.GOOGLE,
        summary="Expired private sleep title",
        start_at=datetime(2026, 8, 8, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 8, 7, tzinfo=UTC),
        is_agent_created=True,
        healthmes_kind="actual_sleep",
        healthmes_source="oura",
        healthmes_source_key="oura:2026-08-08",
        sleep_local_date=datetime(2026, 8, 8, tzinfo=UTC).date(),
        sleep_duration_minutes=420,
        sleep_time_in_bed_minutes=450,
    )
    session.add(row)
    session.flush()

    async def reader(day):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "actual_sleep": {
                "status": "ok",
                "local_date": day.isoformat(),
                "start": row.start_at.isoformat(),
                "wake_time": row.end_at.isoformat(),
                "duration_minutes": 420,
                "source": "oura",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": str(row.id),
                    "source_provider": "healthmes-calendar-mirror",
                    "upstream_provider": "oura",
                    "resource_type": "actual_sleep",
                    "observed_at": row.end_at.isoformat(),
                    "schema_version": 1,
                    "derived_by": "healthmes.actual-sleep-mirror.v1",
                }
            ],
            "freshness": {
                "recorded_at": row.end_at.isoformat(),
                "status": "current",
            },
            "coverage": {"ratio": 1.0},
            "limitations": [],
        }

    provider = WearableContextProvider(
        reader,
        snapshot_session_factory=sessionmaker(
            bind=session.get_bind(),
            expire_on_commit=False,
        ),
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="wearable"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.sleep",
            timezone="UTC",
            parameters={"date": "2026-08-08"},
        ),
    )

    assert session.get(CalendarEventMirror, row.id) is row
    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["source_ref_record_missing"]


@pytest.mark.parametrize(
    ("date_value", "timezone", "expected_hours"),
    (
        ("2025-03-09", "America/New_York", 23),
        ("2025-11-02", "America/New_York", 25),
    ),
)
async def test_local_date_bounds_preserve_dst_days(
    session,
    date_value,
    timezone,
    expected_hours,
):
    provider = StaticProvider(max_lookback_days=1)
    _, turn = _turn(
        provider,
        request=_request(timezone=timezone),
    )

    result = await turn.query(
        session,
        _query(
            timezone=timezone,
            date_value=date_value,
        ),
    )

    assert result.status is ContextStatus.OK
    effective = provider.queries[0]
    assert effective.start is not None
    assert effective.end is not None
    assert (
        effective.end - effective.start
    ).total_seconds() == expected_hours * 3600
    assert "query_range_trimmed" not in result.limitations


async def test_real_activity_provider_accepts_new_york_25_hour_day(
    session,
    monkeypatch,
):
    timezone = "America/New_York"
    selected_day = datetime(2025, 11, 2, tzinfo=UTC).date()
    start, end = local_day_bounds(selected_day, timezone)
    event = _event(
        domain="activity",
        event_type=DAY_SUMMARY_EVENT,
        source_provider="healthmes-activity",
        source_device=None,
        observed_at=start,
        observed_end=end,
        recorded_at=end + timedelta(minutes=1),
        sensitivity="activity-aggregate",
    )
    session.add(event)
    session.flush()

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.activity_summary_context",
        lambda *args, **kwargs: {
            "status": "ok",
            "date": selected_day.isoformat(),
            "timezone": timezone,
            "total_active_minutes": 60,
            "source_coverage": {"ratio": 1.0},
            "evidence_ids": [str(event.id)],
            "freshness": {
                "recorded_at": event.recorded_at.isoformat(),
                "status": "stored_summary",
            },
            "limitations": [],
        },
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((ActivityContextProvider(),)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(timezone=timezone),
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="activity",
            capability="activity.summary",
            timezone=timezone,
            parameters={"date": selected_day.isoformat()},
        ),
    )

    assert result.status is ContextStatus.OK
    assert result.source_refs[0].observed_start == start
    assert result.source_refs[0].observed_end == end
    assert end - start == timedelta(hours=25)


async def test_activity_focus_preserves_window_and_fragmentation_metrics(
    session,
    monkeypatch,
):
    start = datetime(2026, 8, 10, 9, tzinfo=UTC)
    end = datetime(2026, 8, 10, 11, tzinfo=UTC)
    event = _event(
        domain="activity",
        event_type=DAY_SUMMARY_EVENT,
        source_provider="healthmes-activity",
        source_device=None,
        observed_at=start,
        observed_end=end,
        recorded_at=end,
        sensitivity="activity-aggregate",
    )
    session.add(event)
    session.flush()

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.focus_context",
        lambda *args, **kwargs: {
            "status": "ok",
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timezone": "UTC",
            },
            "classification": "fragmented",
            "reason": None,
            "metrics": {
                "total_active_minutes": 90,
                "active_time_range": {
                    "lower_bound_minutes": 90,
                    "upper_bound_minutes": 90,
                    "precision": "exact",
                },
                "app_launches_or_switches": 18,
                "launches_or_switches_per_active_hour": 12,
                "longest_active_block_minutes": 25,
            },
            "boundary": "association_not_causation",
            "coverage": 1.0,
            "evidence_ids": [str(event.id)],
            "freshness": {
                "recorded_at": end.isoformat(),
                "status": "retained_raw_window",
            },
            "limitations": [],
        },
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((ActivityContextProvider(),)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="activity",
            capability="activity.focus",
            start=start,
            end=end,
            granularity="window",
        ),
    )

    assert result.status is ContextStatus.OK
    assert result.payload["window"] == {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": "UTC",
    }
    assert result.payload["metrics"][
        "launches_or_switches_per_active_hour"
    ] == 12
    assert result.payload["metrics"]["active_time_range"] == {
        "lower_bound_minutes": 90,
        "upper_bound_minutes": 90,
        "precision": "exact",
    }
    assert "provider_fields_redacted" not in result.limitations


async def test_provider_freshness_is_recomputed_and_future_values_fail_closed(
    session,
):
    stale_age_provider = StaticProvider(
        result_factory=lambda query, now: ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now - timedelta(minutes=5),
                age_seconds=99_999,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )
    )
    _, age_turn = _turn(stale_age_provider)
    age_result = await age_turn.query(session, _query())

    future_provider = StaticProvider(
        result_factory=lambda query, now: ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now + timedelta(minutes=2),
                age_seconds=0,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )
    )
    _, future_turn = _turn(future_provider)
    future_result = await future_turn.query(session, _query())

    assert age_result.freshness.age_seconds == 300
    assert future_result.status is ContextStatus.DENIED
    assert future_result.limitations == ["freshness_as_of_in_future"]


async def test_gateway_derives_completeness_times_and_rejects_forged_collection(
    session,
):
    observed = datetime(2026, 8, 10, 9, tzinfo=UTC)
    observed_end = datetime(2026, 8, 10, 10, tzinfo=UTC)
    recorded = datetime(2026, 8, 10, 10, 5, tzinfo=UTC)
    event = _event(
        domain="mood",
        observed_at=observed,
        observed_end=observed_end,
        recorded_at=recorded,
    )
    session.add(event)
    session.flush()
    valid_ref = _source_ref(event, domain="mood")

    provider = StaticProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 7},
            source_refs=[valid_ref],
        ).model_copy(
            update={
                "observed_start": NOW - timedelta(days=30),
                "observed_end": NOW - timedelta(days=29),
                "collected_at": NOW - timedelta(days=29),
            }
        )
    )
    _, turn = _turn(provider)

    result = await turn.query(
        session,
        _query(start=observed, end=observed_end),
    )

    assert result.observed_start == observed
    assert result.observed_end == observed_end
    assert result.collected_at == recorded
    assert result.source_refs[0].collected_at is None

    forged_ref = valid_ref.model_copy(
        update={"collected_at": recorded + timedelta(minutes=1)}
    )
    forged_provider = StaticProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 7},
            source_refs=[forged_ref],
        )
    )
    _, forged_turn = _turn(forged_provider)
    forged = await forged_turn.query(
        session,
        _query(start=observed, end=observed_end),
    )

    assert forged.status is ContextStatus.DENIED
    assert forged.limitations == ["source_ref_identity_mismatch"]


async def test_truncated_provider_result_is_explicitly_partial(session):
    provider = StaticProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"records": []},
        ).model_copy(
            update={
                "truncated": True,
                "next_cursor": "next-page",
            }
        )
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    assert result.status is ContextStatus.PARTIAL
    assert result.truncated is True
    assert result.next_cursor == "next-page"
    assert "result_truncated" in result.limitations


async def test_stable_provenance_missing_or_incomplete_is_denied(session):
    missing_provider = StaticProvider(
        provenance=ProvenanceSupport.STABLE,
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 7},
        ),
    )
    _, missing_turn = _turn(missing_provider)
    missing = await missing_turn.query(session, _query())

    unknown_provider = StaticProvider(
        provenance=ProvenanceSupport.STABLE,
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 7},
            status=ContextStatus.PARTIAL,
            freshness=FreshnessStatus.UNKNOWN,
            coverage=CoverageStatus.UNKNOWN,
        ),
    )
    _, unknown_turn = _turn(unknown_provider)
    unknown = await unknown_turn.query(session, _query())

    event = _event(domain="mood")
    session.add(event)
    session.flush()
    incomplete_provider = StaticProvider(
        provenance=ProvenanceSupport.STABLE,
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 7},
            source_refs=[_source_ref(event, domain="mood")],
            limitations=["provenance_incomplete"],
        ),
    )
    _, incomplete_turn = _turn(incomplete_provider)
    incomplete = await incomplete_turn.query(session, _query())

    assert missing.status is ContextStatus.DENIED
    assert set(missing.limitations) == {
        "source_refs_unavailable",
        "stable_provenance_missing",
    }
    assert unknown.status is ContextStatus.DENIED
    assert set(unknown.limitations) == {
        "source_refs_unavailable",
        "stable_provenance_missing",
    }
    assert incomplete.status is ContextStatus.DENIED
    assert incomplete.limitations == ["stable_provenance_incomplete"]


async def test_unbounded_query_gets_a_safe_current_day_boundary(session):
    provider = StaticProvider()
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    effective = provider.queries[0]
    assert result.status is ContextStatus.OK
    assert effective.start == DAY_START
    assert effective.end == NOW + timedelta(seconds=1)


async def test_capability_lookback_is_trimmed_with_its_access_window(session):
    provider = StaticProvider(
        max_lookback_days=90,
        default_lookback_days=7,
        lookback_parameter="lookback_days",
        lookback_parameter_offset_days=1,
        parameters=("date", "lookback_days"),
    )
    _, turn = _turn(
        provider,
        policy=_policy(max_query_days=3),
    )

    result = await turn.query(
        session,
        _query(date_value="2026-08-10").model_copy(
            update={"parameters": {"date": "2026-08-10", "lookback_days": 7}}
        ),
    )

    effective = provider.queries[0]
    assert effective.parameters["lookback_days"] == 2
    assert effective.start == datetime(2026, 8, 8, tzinfo=UTC)
    assert effective.end == NOW + timedelta(seconds=1)
    assert "query_lookback_trimmed" in result.limitations


async def test_fixed_half_hour_timezone_is_resolved_to_utc(session):
    provider = StaticProvider(max_lookback_days=1)
    _, turn = _turn(
        provider,
        request=_request(timezone="UTC+05:30"),
    )

    await turn.query(
        session,
        _query(
            timezone="UTC+05:30",
            date_value="2026-08-09",
        ),
    )

    effective = provider.queries[0]
    assert effective.start == datetime(
        2026,
        8,
        8,
        18,
        30,
        tzinfo=UTC,
    )
    assert effective.end == datetime(
        2026,
        8,
        9,
        18,
        30,
        tzinfo=UTC,
    )


async def test_future_context_is_trimmed_or_denied_by_capability(session):
    past_only = StaticProvider(max_lookback_days=1)
    _, current_turn = _turn(past_only)

    current = await current_turn.query(
        session,
        _query(date_value="2026-08-10"),
    )
    future = await ContextAccessLayer(
        ContextProviderRegistry((past_only,)),
        clock=lambda: NOW,
    ).start_turn(
        _request(),
        policy=_policy(),
    ).query(
        session,
        _query(date_value="2026-08-11"),
    )

    assert current.status is ContextStatus.PARTIAL
    assert current.limitations == ["future_range_trimmed"]
    assert past_only.queries[0].end == NOW + timedelta(seconds=1)
    assert future.status is ContextStatus.DENIED
    assert future.limitations == ["future_context_unavailable"]


async def test_calendar_capability_can_read_future_context(session):
    provider = StaticProvider(
        provider_id="calendar",
        domain="calendar",
        allows_future=True,
        max_lookback_days=1,
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="calendar"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="calendar",
            domain="calendar",
            date_value="2026-08-11",
        ),
    )

    assert result.status is ContextStatus.OK
    assert provider.queries[0].start == DAY_END
    assert provider.queries[0].end == DAY_END + timedelta(days=1)


async def test_query_range_is_calendar_trimmed_or_denied(session):
    provider = StaticProvider(max_lookback_days=2)
    start = datetime(2026, 8, 1, 12, tzinfo=UTC)
    end = datetime(2026, 8, 5, 12, tzinfo=UTC)
    trim_policy = _policy(max_query_days=2)
    _, trim_turn = _turn(provider, policy=trim_policy)
    _, deny_turn = _turn(
        provider,
        policy=_policy(
            max_query_days=2,
            trim_overlong_queries=False,
        ),
    )

    trimmed = await trim_turn.query(
        session,
        _query(start=start, end=end),
    )
    denied = await deny_turn.query(
        session,
        _query(start=start, end=end),
    )

    assert trimmed.status is ContextStatus.PARTIAL
    assert "query_range_trimmed" in trimmed.limitations
    assert provider.queries[0].start == datetime(
        2026,
        8,
        4,
        tzinfo=UTC,
    )
    assert denied.limitations == ["query_range_exceeds_limit"]


async def test_provider_rows_cannot_exceed_effective_limit(session):
    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"records": [{"value": index} for index in range(3)]},
        )

    provider = StaticProvider(
        limit_output_fields=("records",),
        result_factory=result_factory,
    )
    _, turn = _turn(
        provider,
        policy=_policy(max_rows_per_query=2),
    )

    result = await turn.query(session, _query(limit=10))

    assert provider.queries[0].limit == 2
    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["result_rows_exceed_limit"]


async def test_payload_and_source_ref_per_query_budgets_fail_closed(
    session,
):
    large_provider = StaticProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": "x" * 2_000},
        )
    )
    _, large_turn = _turn(
        large_provider,
        policy=_policy(max_payload_bytes_per_query=1_024),
    )

    large = await large_turn.query(session, _query())

    first = _event(
        domain="nutrition",
        source_record_id="budget-first",
    )
    second = _event(
        domain="nutrition",
        source_record_id="budget-second",
        observed_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
    )
    session.add_all((first, second))
    session.flush()

    def refs_result(query, now):
        return _result(
            query,
            now=now,
            payload={"value": 2},
            source_refs=[
                _source_ref(first, domain="nutrition"),
                _source_ref(second, domain="nutrition"),
            ],
        )

    refs_provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=refs_result,
    )
    _, refs_turn = _turn(
        refs_provider,
        policy=_policy(
            domain="nutrition",
            max_source_refs_per_query=1,
        ),
    )
    too_many_refs = await refs_turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert large.limitations == ["result_payload_exceeds_limit"]
    assert too_many_refs.limitations == [
        "result_source_refs_exceed_limit"
    ]


async def test_turn_tool_and_source_ref_budgets_accumulate(session):
    first = _event(
        domain="nutrition",
        source_record_id="turn-first",
    )
    second = _event(
        domain="nutrition",
        source_record_id="turn-second",
        observed_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
    )
    session.add_all((first, second))
    session.flush()
    refs = iter((first, second))

    def result_factory(query, now):
        event = next(refs)
        return _result(
            query,
            now=now,
            payload={"value": 1},
            source_refs=[_source_ref(event, domain="nutrition")],
        )

    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=result_factory,
    )
    request = _request(
        budget=DecisionBudget(
            max_tool_calls=2,
            max_source_refs=1,
        )
    )
    _, turn = _turn(
        provider,
        request=request,
        policy=_policy(domain="nutrition"),
    )
    query = _query(
        provider_id="nutrition",
        domain="nutrition",
        start=DAY_START,
        end=NOW,
    )

    first_result = await turn.query(session, query)
    second_result = await turn.query(
        session,
        query.model_copy(update={"query_id": uuid.uuid4()}),
    )
    third_result = await turn.query(
        session,
        query.model_copy(update={"query_id": uuid.uuid4()}),
    )

    assert first_result.status is ContextStatus.OK
    assert second_result.limitations == [
        "turn_source_ref_budget_exhausted"
    ]
    assert third_result.limitations == [
        "turn_tool_call_budget_exhausted"
    ]
    assert len(provider.queries) == 2


async def test_turn_context_byte_budget_accumulates(session):
    provider = StaticProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": "x" * 550},
        )
    )
    request = _request(
        budget=DecisionBudget(max_context_bytes=1_024)
    )
    _, turn = _turn(
        provider,
        request=request,
        policy=_policy(max_payload_bytes_per_query=2_000),
    )
    first_query = _query()

    first = await turn.query(session, first_query)
    second = await turn.query(
        session,
        first_query.model_copy(update={"query_id": uuid.uuid4()}),
    )

    assert first.status is ContextStatus.PARTIAL
    assert second.limitations == [
        "turn_context_byte_budget_exhausted"
    ]


async def test_parallel_calls_cannot_overspend_context_byte_budget(session):
    provider = BarrierProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": "x" * 550},
        )
    )
    request = _request(
        budget=DecisionBudget(max_context_bytes=1_024)
    )
    _, turn = _turn(
        provider,
        request=request,
        policy=_policy(max_payload_bytes_per_query=2_000),
    )
    first_query = _query()
    first, second = await asyncio.gather(
        turn.query(session, first_query),
        turn.query(
            session,
            first_query.model_copy(update={"query_id": uuid.uuid4()}),
        ),
    )

    results = (first, second)
    assert sum(
        result.status is ContextStatus.DENIED for result in results
    ) == 1
    assert any(
        result.limitations == ["turn_context_byte_budget_exhausted"]
        for result in results
    )
    assert turn.context_bytes_used <= request.budget.max_context_bytes


async def test_parallel_calls_cannot_overspend_source_ref_budget(session):
    event = _event(domain="mood")
    session.add(event)
    session.flush()
    provider = BarrierProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 7},
            source_refs=[_source_ref(event, domain="mood")],
        )
    )
    request = _request(
        budget=DecisionBudget(max_source_refs=1)
    )
    _, turn = _turn(provider, request=request)
    first_query = _query()
    first, second = await asyncio.gather(
        turn.query(session, first_query),
        turn.query(
            session,
            first_query.model_copy(update={"query_id": uuid.uuid4()}),
        ),
    )

    results = (first, second)
    assert sum(
        result.status is ContextStatus.DENIED for result in results
    ) == 1
    assert any(
        result.limitations == ["turn_source_ref_budget_exhausted"]
        for result in results
    )
    assert turn.source_refs_used == 1


async def test_unknown_freshness_and_coverage_remain_unknown(session):
    provider = StaticProvider(
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 7},
            freshness=FreshnessStatus.UNKNOWN,
            coverage=CoverageStatus.UNKNOWN,
        )
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    assert result.status is ContextStatus.PARTIAL
    assert result.freshness.status is FreshnessStatus.UNKNOWN
    assert result.freshness.as_of is None
    assert result.coverage.status is CoverageStatus.UNKNOWN
    assert result.coverage.ratio is None
    assert {
        "coverage_unknown",
        "freshness_unknown",
        "source_refs_unavailable",
    } <= set(result.limitations)


async def test_malicious_provider_contract_output_fails_closed(session):
    provider = StaticProvider(
        result_factory=lambda query, now: {
            "query_id": str(query.query_id),
            "payload": {"value": "not typed"},
        }
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    assert result.status is ContextStatus.FAILED
    assert result.payload == {}
    assert result.limitations == ["provider_contract_violation"]
    assert turn.trace[0].outcome is AccessOutcome.PARTIAL


async def test_failed_provider_refs_are_discarded_and_audit_text_is_safe(
    session,
):
    event = _event(domain="mood")
    session.add(event)
    session.flush()
    provider = StaticProvider(
        result_factory=lambda query, now: ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.FAILED,
            source_refs=[_source_ref(event, domain="mood")],
            freshness=ContextFreshness(
                status=FreshnessStatus.UNAVAILABLE
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.UNAVAILABLE
            ),
            limitations=["private provider detail"],
        )
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    assert result.status is ContextStatus.FAILED
    assert result.source_refs == []
    assert "provider_source_refs_discarded" in result.limitations
    assert "private provider detail" not in result.model_dump_json()
    assert "provider_reported_limitation" in result.limitations
    audit = turn.trace[0].model_dump_json()
    assert "private provider detail" not in audit
    assert turn.trace[0].reason_codes == (
        "provider_reported_limitation",
        "provider_source_refs_discarded",
    )


async def test_provider_output_is_allowlisted_before_privacy_filtering(
    session,
):
    provider = StaticProvider(
        output_fields=("value",),
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={
                "value": 7,
                "undeclared": "must not cross the gateway",
                "app_name": "Private App",
            },
        ),
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    assert result.payload == {"value": 7}
    assert "provider_fields_redacted" in result.limitations
    assert turn.trace[0].redacted_paths == (
        "app_name",
        "provider_field",
    )
    audit = turn.trace[0].model_dump_json()
    assert "must not cross the gateway" not in audit
    assert "Private App" not in audit


async def test_provider_cannot_mutate_canonical_query_allowlist(session):
    class MutatingProvider(StaticProvider):
        async def query(self, session, query, *, now):
            del session
            query.fields.append("secret")
            self.queries.append(query)
            return _result(
                query,
                now=now,
                payload={
                    "value": 7,
                    "secret": "provider-private-value",
                },
            )

    provider = MutatingProvider(output_fields=("value",))
    _, turn = _turn(provider)
    query = _query().model_copy(update={"fields": ["value"]})

    result = await turn.query(session, query)

    assert provider.queries[0].fields == ["value", "secret"]
    assert query.fields == ["value"]
    assert result.payload == {"value": 7}
    assert "provider-private-value" not in result.model_dump_json()
    assert "provider_fields_redacted" in result.limitations


async def test_forged_source_domain_is_denied(session):
    event = _event(domain="nutrition")
    session.add(event)
    session.flush()
    forged = SourceRef(
        domain="activity",
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=event.source_provider,
        observed_start=event.observed_at,
        schema_version=1,
        derived_by="activity.test.v1",
        freshness=FreshnessStatus.CURRENT,
        coverage=event.coverage,
        sensitivity=event.sensitivity,
    )

    def result_factory(query, now):
        return _result(
            query,
            now=now,
            payload={"value": 1},
            source_refs=[forged],
        )

    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=result_factory,
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.limitations == ["source_ref_domain_mismatch"]


async def test_query_timezone_must_match_decision_turn(session):
    provider = StaticProvider()
    _, turn = _turn(
        provider,
        request=_request(timezone="Asia/Seoul"),
    )

    result = await turn.query(
        session,
        _query(timezone="UTC"),
    )

    assert result.limitations == ["query_timezone_mismatch"]
    assert provider.queries == []


@pytest.mark.parametrize(
    ("query_update", "reason"),
    (
        (
            {"granularity": "record"},
            "query_granularity_unsupported",
        ),
        (
            {"fields": ["undeclared"]},
            "query_fields_unsupported",
        ),
        (
            {"parameters": {"undeclared": True}},
            "query_parameters_unsupported",
        ),
    ),
)
async def test_capability_contract_is_enforced_before_provider_execution(
    session,
    query_update,
    reason,
):
    provider = StaticProvider()
    _, turn = _turn(provider)
    query = _query().model_copy(update=query_update)

    result = await turn.query(session, query)

    assert result.status is ContextStatus.DENIED
    assert result.limitations == [reason]
    assert provider.queries == []


async def test_parameter_types_are_enforced_before_provider_execution(
    session,
):
    provider = StaticProvider(parameters=("confirmed_only",))
    _, turn = _turn(provider)
    query = _query().model_copy(
        update={"parameters": {"confirmed_only": "false"}}
    )

    result = await turn.query(session, query)

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["query_parameters_invalid"]
    assert provider.queries == []


async def test_forged_external_wearable_identity_is_denied(session):
    forged = SourceRef(
        domain="wearable",
        resource_type="untrusted_score",
        record_id="score-1",
        source_provider="open-wearables",
        observed_start=datetime(2026, 8, 9, 8, tzinfo=UTC),
        schema_version=1,
        derived_by="open-wearables.daily-readiness.v1",
        freshness=FreshnessStatus.CURRENT,
        sensitivity="wearable",
    )
    provider = StaticProvider(
        provider_id="wearable",
        domain="wearable",
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 80},
            source_refs=[forged],
        ),
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="wearable"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="wearable",
            domain="wearable",
            date_value="2026-08-09",
        ),
    )

    assert result.limitations == [
        "external_source_identity_mismatch"
    ]


async def test_open_wearables_identity_contract_rejects_forged_variants(
    session,
):
    observed = datetime(2026, 8, 9, tzinfo=UTC)
    invalid_refs = (
        SourceRef(
            domain="wearable",
            resource_type="unknown",
            record_id="unknown-1",
            source_provider="open-wearables",
            observed_start=observed,
            schema_version=1,
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="wearable",
        ),
        SourceRef(
            domain="wearable",
            resource_type="health_score",
            record_id="score-schema",
            source_provider="open-wearables",
            observed_start=observed,
            schema_version=2,
            derived_by="open-wearables.daily-readiness.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="wearable",
        ),
        SourceRef(
            domain="wearable",
            resource_type="health_score",
            record_id="score-sensitive",
            source_provider="open-wearables",
            observed_start=observed,
            schema_version=1,
            derived_by="open-wearables.daily-readiness.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="private",
        ),
        SourceRef(
            domain="wearable",
            resource_type="health_score",
            record_id="score-window",
            source_provider="open-wearables",
            observed_start=observed,
            observed_end=observed + timedelta(hours=1),
            schema_version=1,
            derived_by="open-wearables.daily-readiness.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="wearable",
        ),
        SourceRef(
            domain="wearable",
            resource_type="sleep_summary",
            record_id="sleep-missing-end",
            source_provider="open-wearables",
            observed_start=observed,
            schema_version=1,
            derived_by="open-wearables.daily-readiness.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="wearable",
        ),
        SourceRef(
            domain="wearable",
            resource_type="sleep_summary",
            record_id="sleep-short",
            source_provider="open-wearables",
            observed_start=observed,
            observed_end=observed + timedelta(hours=21),
            schema_version=1,
            derived_by="open-wearables.daily-readiness.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="wearable",
        ),
        SourceRef(
            domain="wearable",
            resource_type="sleep_summary",
            record_id="sleep-long",
            source_provider="open-wearables",
            observed_start=observed,
            observed_end=observed + timedelta(hours=27),
            schema_version=1,
            derived_by="open-wearables.daily-readiness.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="wearable",
        ),
    )

    for index, ref in enumerate(invalid_refs):
        provider = StaticProvider(
            provider_id="wearable",
            domain="wearable",
            result_factory=lambda query, now, current=ref: _result(
                query,
                now=now,
                payload={"value": 80},
                source_refs=[current],
            ),
        )
        _, turn = _turn(
            provider,
            policy=_policy(domain="wearable"),
        )

        result = await turn.query(
            session,
            _query(
                provider_id="wearable",
                domain="wearable",
                date_value="2026-08-09",
            ).model_copy(update={"query_id": uuid.uuid4()}),
        )

        assert result.limitations == [
            "external_source_identity_mismatch"
        ], index


async def test_open_wearables_lookback_uses_local_calendar_days(session):
    timezone = "America/New_York"
    allowed_start, _ = local_day_bounds(
        datetime(2025, 10, 31, tzinfo=UTC).date(),
        timezone,
    )
    denied_start, _ = local_day_bounds(
        datetime(2025, 10, 30, tzinfo=UTC).date(),
        timezone,
    )

    def wearable_ref(record_id: str, observed_start: datetime) -> SourceRef:
        return SourceRef(
            domain="wearable",
            resource_type="health_score",
            record_id=record_id,
            source_provider="open-wearables",
            observed_start=observed_start + timedelta(hours=12),
            schema_version=1,
            derived_by="open-wearables.daily-readiness.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="wearable",
        )

    refs = (
        wearable_ref("allowed-local-day", allowed_start),
        wearable_ref("denied-local-day", denied_start),
    )
    results = []
    for ref in refs:
        provider = StaticProvider(
            provider_id="wearable",
            domain="wearable",
            result_factory=lambda query, now, current=ref: _result(
                query,
                now=now,
                payload={"value": 80},
                source_refs=[current],
            ),
        )
        _, turn = _turn(
            provider,
            request=_request(timezone=timezone),
            policy=_policy(domain="wearable"),
        )
        results.append(
            await turn.query(
                session,
                _query(
                    provider_id="wearable",
                    domain="wearable",
                    timezone=timezone,
                    date_value="2025-11-02",
                ),
            )
        )

    assert results[0].status is ContextStatus.PARTIAL
    assert results[1].status is ContextStatus.DENIED
    assert results[1].limitations == ["source_ref_outside_query"]


async def test_calendar_mirror_ref_requires_exact_end_and_identity(session):
    row = CalendarEventMirror(
        external_id="calendar-access-1",
        calendar_source=CalendarSource.GOOGLE,
        summary="Private title",
        start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        is_all_day=False,
    )
    session.add(row)
    session.flush()
    missing_end = SourceRef(
        domain="calendar",
        resource_type="calendar.event",
        record_id=str(row.id),
        source_provider="healthmes-calendar-mirror",
        observed_start=row.start_at,
        schema_version=1,
        derived_by="calendar.context.v1",
        freshness=FreshnessStatus.UNKNOWN,
        sensitivity="calendar-metadata",
    )
    provider = StaticProvider(
        provider_id="calendar",
        domain="calendar",
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 1},
            source_refs=[missing_end],
        ),
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="calendar"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="calendar",
            domain="calendar",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["source_ref_observation_mismatch"]


async def test_calendar_source_ref_rejects_expired_row_before_maintenance(
    session,
):
    update_retention_policy(
        session,
        "calendar_mirror",
        "1d",
        now=NOW,
    )
    row = CalendarEventMirror(
        external_id="calendar-expired-source-ref",
        calendar_source=CalendarSource.GOOGLE,
        summary="Expired but not yet maintained",
        start_at=datetime(2026, 8, 8, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 8, 10, tzinfo=UTC),
        is_all_day=False,
    )
    session.add(row)
    session.flush()
    ref = SourceRef(
        domain="calendar",
        resource_type="calendar.event",
        record_id=str(row.id),
        source_provider="healthmes-calendar-mirror",
        observed_start=row.start_at,
        observed_end=row.end_at,
        schema_version=1,
        derived_by="calendar.context.v1",
        freshness=FreshnessStatus.UNKNOWN,
        sensitivity="calendar-metadata",
    )
    provider = StaticProvider(
        provider_id="calendar",
        domain="calendar",
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 1},
            source_refs=[ref],
        ),
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="calendar"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="calendar",
            domain="calendar",
            start=datetime(2026, 8, 8, tzinfo=UTC),
            end=NOW,
        ),
    )

    assert session.get(CalendarEventMirror, row.id) is row
    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["source_ref_record_missing"]


async def test_naive_stored_observation_window_is_rejected(session):
    event = _event(domain="nutrition")
    event.payload = {
        "window": {
            "start": event.observed_at.isoformat(),
            "end": "2026-08-10T10:00:00",
        }
    }
    session.add(event)
    session.flush()
    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 1},
            source_refs=[
                _source_ref(
                    event,
                    domain="nutrition",
                    observed_end=None,
                )
            ],
        ),
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["source_ref_identity_mismatch"]


async def test_unknown_local_source_freshness_is_not_upgraded(session):
    event = _event(domain="nutrition")
    session.add(event)
    session.flush()
    ref = _source_ref(event, domain="nutrition").model_copy(
        update={"freshness": FreshnessStatus.UNKNOWN}
    )
    provider = StaticProvider(
        provider_id="nutrition",
        domain="nutrition",
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={"value": 1},
            source_refs=[ref],
        ),
    )
    _, turn = _turn(
        provider,
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="nutrition",
            domain="nutrition",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.source_refs[0].freshness is FreshnessStatus.UNKNOWN


async def test_real_wearable_provider_preserves_actual_sleep_interval(
    session,
    tmp_path,
):
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path / "data",
        timezone="UTC",
        _env_file=None,
    )
    token_path = settings.data_dir / "google" / "calendar_token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": "actual-sleep-refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        ),
        encoding="utf-8",
    )
    account_generation = creds.calendar_account_generation(
        settings,
        CalendarSource.GOOGLE,
    )
    assert account_generation is not None
    row = CalendarEventMirror(
        external_id="actual-sleep-access-1",
        calendar_source=CalendarSource.GOOGLE,
        connection_generation=account_generation,
        summary="Private sleep title",
        start_at=datetime(2026, 8, 10, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 7, tzinfo=UTC),
        is_agent_created=True,
        healthmes_kind="actual_sleep",
        healthmes_source="oura",
        healthmes_source_key="oura:2026-08-10",
        observation_fingerprint="sleep-fingerprint",
        sleep_local_date=datetime(2026, 8, 10, tzinfo=UTC).date(),
        sleep_duration_minutes=420,
        sleep_time_in_bed_minutes=450,
    )
    session.add(row)
    session.flush()

    async def reader(day):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "actual_sleep": {
                "status": "ok",
                "local_date": day.isoformat(),
                "start": row.start_at.isoformat(),
                "wake_time": row.end_at.isoformat(),
                "duration_minutes": 420,
                "source": "oura",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": str(row.id),
                    "source_provider": "healthmes-calendar-mirror",
                    "upstream_provider": "oura",
                    "resource_type": "actual_sleep",
                    "observed_at": row.end_at.isoformat(),
                    "schema_version": 1,
                    "derived_by": "healthmes.actual-sleep-mirror.v1",
                }
            ],
            "freshness": {
                "recorded_at": row.end_at.isoformat(),
                "status": "current",
            },
            "coverage": {"ratio": 1.0},
            "limitations": [],
        }

    provider = WearableContextProvider(
        reader,
        snapshot_session_factory=sessionmaker(
            bind=session.get_bind(),
            expire_on_commit=False,
        ),
    )
    health = InMemorySyncHealthStore()
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
        calendar_settings=settings,
        calendar_sync_health_store=health,
    )
    blocked_turn = layer.start_turn(
        _request(),
        policy=_policy(domain="wearable"),
    )
    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.sleep",
        parameters={"date": "2026-08-10"},
    )

    blocked = await blocked_turn.query(session, query)

    assert blocked.status is ContextStatus.DENIED
    assert blocked.source_refs == []
    assert "calendar_account_not_synced" in blocked.limitations

    health.record_success(
        CalendarSource.GOOGLE,
        NOW,
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=account_generation,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="wearable"),
    )
    result = await turn.query(
        session,
        query,
    )

    assert result.status is ContextStatus.PARTIAL
    assert len(result.source_refs) == 1
    source_ref = result.source_refs[0]
    assert source_ref.record_id != str(row.id)
    assert (
        source_ref.source_provider
        == "healthmes-open-wearables-mirror"
    )
    assert source_ref.observed_start == DAY_START
    assert source_ref.observed_end == DAY_END
    assert source_ref.freshness is FreshnessStatus.CURRENT
    assert source_ref.content_digest is not None
    assert "Private sleep title" not in result.model_dump_json()


async def test_real_nutrition_provider_redacts_identity_from_aggregate(
    session,
    monkeypatch,
):
    interaction_id = uuid.uuid4().hex
    interaction_event = _event(
        domain="nutrition",
        event_type="nutrition.interaction.v1",
        source_provider="nutrition-interaction",
        source_record_id=interaction_id,
    )
    observation_event = _event(
        domain="nutrition",
        event_type="nutrition.observation.v1",
        source_provider="sake-vlm",
        source_record_id=interaction_id,
    )
    session.add_all((interaction_event, observation_event))
    session.flush()

    def history(*args, **kwargs):
        return {
            "status": "ok",
            "count": 1,
            "records": [
                {
                    "interaction_id": interaction_id,
                    "nutrition_observation_id": interaction_id,
                    "recorded_at": NOW.isoformat(),
                    "question": "Can I eat this private meal?",
                    "resolved_items": [
                        {
                            "name": "Private meal name",
                            "evidence_text": "Private package label",
                            "nutrients": [
                                {
                                    "nutrient": "caffeine",
                                    "amount": 80,
                                    "unit": "mg",
                                }
                            ],
                        }
                    ],
                }
            ],
            "truncated": False,
            "coverage": {"complete": True},
        }

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.search_intake_history",
        history,
    )
    provider = NutritionContextProvider()
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="nutrition"),
    )
    result = await turn.query(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.intake-history",
            start=DAY_START,
            end=NOW,
            limit=10,
        ),
    )

    serialized = result.model_dump_json()
    assert result.status is ContextStatus.PARTIAL
    assert {ref.record_id for ref in result.source_refs} == {
        str(interaction_event.id),
        str(observation_event.id),
    }
    assert "Private meal name" not in serialized
    assert "Can I eat this private meal?" not in serialized
    assert "Private package label" not in serialized
    assert interaction_id not in serialized
    assert result.payload["records"][0]["resolved_items"][0]["nutrients"] == [
        {
            "nutrient": "caffeine",
            "amount": 80,
            "unit": "mg",
        }
    ]


async def test_stable_provider_can_return_reference_free_no_data(session):
    provider = StaticProvider(
        output_fields=("status", "value"),
        provenance=ProvenanceSupport.STABLE,
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={
                "status": "insufficient_data",
                "value": None,
            },
            status=ContextStatus.PARTIAL,
            freshness=FreshnessStatus.UNKNOWN,
            coverage=CoverageStatus.UNKNOWN,
        ),
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query())

    assert result.status is ContextStatus.PARTIAL
    assert result.payload == {"status": "insufficient_data"}
    assert "stable_provenance_missing" not in result.limitations
    assert "source_refs_unavailable" in result.limitations
    assert "reference_free_no_data_sanitized" in result.limitations
    assert (
        "reference_free_no_data_sanitized"
        in turn.trace[0].reason_codes
    )


async def test_empty_nutrition_history_returns_reference_free_no_data(
    session,
):
    layer = ContextAccessLayer(
        ContextProviderRegistry((NutritionContextProvider(),)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.intake-history",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload == {"status": "insufficient_data"}
    assert result.source_refs == []
    assert "stable_provenance_missing" not in result.limitations
    assert "reference_free_no_data_sanitized" in result.limitations


async def test_nutrition_provenance_ids_are_bound_to_event_type(
    session,
    monkeypatch,
):
    interaction_id = str(uuid.uuid4())
    interaction = _event(
        domain="nutrition",
        event_type="nutrition.interaction.v1",
        source_provider="nutrition-interaction",
        source_record_id=interaction_id,
    )
    session.add(interaction)
    session.flush()

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.search_intake_history",
        lambda *args, **kwargs: {
            "status": "ok",
            "count": 1,
            "records": [
                {
                    "interaction_id": interaction_id,
                    "nutrition_observation_id": interaction_id,
                    "observed_at": interaction.observed_at.isoformat(),
                    "recorded_at": interaction.recorded_at.isoformat(),
                    "resolved_items": [],
                    "is_confirmed_intake": False,
                }
            ],
            "truncated": False,
            "coverage": {"complete": True},
        },
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((NutritionContextProvider(),)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.intake-history",
            start=DAY_START,
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.source_refs == []
    assert "stable_provenance_incomplete" in result.limitations


async def test_future_nutrition_decision_snapshot_supports_90_local_days(
    session,
    monkeypatch,
):
    timezone = "America/New_York"
    zone = ZoneInfo(timezone)
    start = datetime(2026, 8, 9, 8, tzinfo=zone)
    intended_at = datetime(2026, 11, 7, 8, tzinfo=zone)
    request_id = uuid.uuid4()
    event = _event(
        domain="nutrition",
        event_type="nutrition.decision-request.v1",
        source_provider="nutrition-decision-request",
        source_record_id=str(request_id),
        observed_at=NOW,
        recorded_at=NOW,
    )
    session.add(event)
    session.flush()

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.nutrition_decision_context",
        lambda *args, **kwargs: {
            "status": "ok",
            "request": {
                "request_id": str(request_id),
                "scope": "daily_nutrition",
                "question": "Should I plan this meal?",
                "requested_at": NOW.isoformat(),
                "intended_consumption_at": intended_at.isoformat(),
            },
            "candidate": {
                "is_confirmed_intake": False,
                "resolved_items": [],
            },
            "comparison_candidates": [],
            "confirmed_intake_history": [],
            "history_window": {
                "start": start.isoformat(),
                "end": intended_at.isoformat(),
                "lookback_days": 90,
                "coverage": "captured_records_only",
                "query": {"complete": True},
            },
            "specialized_evidence": {"caffeine": None},
            "evidence_event_ids": [str(event.id)],
            "boundaries": {
                "candidate_is_not_consumed": True,
                "history_is_not_complete_day_proof": True,
                "medical_safety_requires_separate_policy": True,
            },
        },
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((NutritionContextProvider(),)),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(timezone=timezone),
        policy=_policy(domain="nutrition"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.decision-context",
            start=start,
            end=intended_at,
            timezone=timezone,
            parameters={"request_id": str(request_id)},
        ),
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["history_window"]["start"] == start.isoformat()
    assert (
        result.payload["history_window"]["end"]
        == intended_at.isoformat()
    )
    assert (
        turn.trace[0].effective_end
        == intended_at.astimezone(UTC)
    )
    assert "future_range_trimmed" not in result.limitations


async def test_limit_applies_only_to_declared_result_collection(session):
    provider = StaticProvider(
        output_fields=("records",),
        nested_output_fields=("nutrients", "amount"),
        limit_output_fields=("records",),
        result_factory=lambda query, now: _result(
            query,
            now=now,
            payload={
                "records": [
                    {
                        "nutrients": [
                            {"amount": 1},
                            {"amount": 2},
                        ]
                    }
                ]
            },
        ),
    )
    _, turn = _turn(provider)

    result = await turn.query(session, _query(limit=1))

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["records"][0]["nutrients"] == [
        {"amount": 1},
        {"amount": 2},
    ]
    assert "result_rows_exceed_limit" not in result.limitations


async def test_explicit_date_must_match_bounded_range(session):
    provider = StaticProvider()
    _, turn = _turn(provider)

    result = await turn.query(
        session,
        _query(
            start=DAY_START,
            end=NOW,
            date_value="2026-08-09",
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["query_date_range_mismatch"]
    assert provider.queries == []


async def test_finished_revoked_activity_does_not_block_later_window(session):
    revoked = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="revoked-old-device",
        observed_at=datetime(2026, 8, 10, 8, tzinfo=UTC),
        observed_end=datetime(2026, 8, 10, 9, tzinfo=UTC),
        sensitivity="activity-identity",
    )
    selected = _event(
        domain="activity",
        event_type=APP_INTERVAL_EVENT,
        source_provider="activitywatch",
        source_device="selected-current-device",
        observed_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        observed_end=NOW,
        sensitivity="activity-identity",
    )
    session.add_all((revoked, selected))
    session.flush()
    update_collection_status(
        session,
        "revoked-old-device",
        ActivityCollectionStatusUpdate(
            permission_status=ActivityPermissionStatus.REVOKED,
            status_observed_at=NOW,
        ),
        now=NOW,
    )
    provider = _activity_provider_for(selected)
    _, turn = _turn(
        provider,
        policy=_policy(domain="activity"),
    )

    result = await turn.query(
        session,
        _query(
            provider_id="activity",
            domain="activity",
            start=datetime(2026, 8, 10, 11, tzinfo=UTC),
            end=NOW,
        ),
    )

    assert result.status is ContextStatus.OK
    assert result.source_refs[0].record_id == str(selected.id)


async def test_calendar_day_summary_uses_requested_partial_window(session):
    morning = CalendarEventMirror(
        external_id="calendar-morning",
        calendar_source=CalendarSource.GOOGLE,
        summary="Morning title",
        start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        is_all_day=False,
        created_at=NOW,
        updated_at=NOW,
    )
    afternoon = CalendarEventMirror(
        external_id="calendar-afternoon",
        calendar_source=CalendarSource.GOOGLE,
        summary="Afternoon title",
        start_at=datetime(2026, 8, 10, 15, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 16, tzinfo=UTC),
        is_all_day=False,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add_all((morning, afternoon))
    session.flush()
    layer = ContextAccessLayer(
        ContextProviderRegistry(
            (
                CalendarContextProvider(
                    sources=(CalendarSource.GOOGLE,),
                ),
            )
        ),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="calendar"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            start=datetime(2026, 8, 10, 8, tzinfo=UTC),
            end=datetime(2026, 8, 10, 11, tzinfo=UTC),
        ),
    )

    assert result.payload["event_count"] == 1
    assert result.payload["busy_minutes"] == 60
    assert result.source_refs[0].record_id.startswith("aggregate:v1:")
    assert str(morning.id) not in result.model_dump_json()
    assert str(afternoon.id) not in result.model_dump_json()


async def test_calendar_empty_success_metadata_survives_access_gateway(
    session,
):
    health = InMemorySyncHealthStore()
    collected_at = NOW - timedelta(minutes=1)
    health.record_success(
        CalendarSource.GOOGLE,
        collected_at,
        event_count=0,
        coverage_kind=SyncCoverageKind.BOUNDED_WINDOW,
        coverage_start=DAY_START,
        coverage_end=DAY_END,
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry(
            (
                CalendarContextProvider(
                    sync_health_store=health,
                    sources=(CalendarSource.GOOGLE,),
                ),
            )
        ),
        clock=lambda: NOW,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="calendar"),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            start=DAY_START,
            end=DAY_END,
        ),
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["status"] == "empty_success"
    assert result.payload["event_count"] == 0
    assert result.payload["busy_minutes"] == 0
    assert len(result.source_refs) == 1
    assert result.source_refs[0].record_id.startswith("aggregate:v1:")
    assert result.observed_start == DAY_START
    assert result.observed_end == DAY_END
    assert result.collected_at == collected_at
    assert result.coverage.status is CoverageStatus.COMPLETE
    assert "source_refs_unavailable" not in result.limitations
    assert "stable_provenance_missing" not in result.limitations


async def test_calendar_aggregate_identity_uses_only_visible_current_generations(
    session,
):
    google_generation = "a" * 64
    stale_google_generation = "b" * 64
    caldav_generation = "c" * 64
    stale_caldav_generation = "d" * 64
    connected = {
        CalendarSource.GOOGLE,
        CalendarSource.CALDAV,
    }
    generations = {
        CalendarSource.GOOGLE: google_generation,
        CalendarSource.CALDAV: caldav_generation,
    }
    health = InMemorySyncHealthStore()
    health.record_success(
        CalendarSource.GOOGLE,
        NOW - timedelta(minutes=1),
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=google_generation,
    )
    health.record_success(
        CalendarSource.CALDAV,
        NOW - timedelta(minutes=1),
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=stale_caldav_generation,
    )
    session.add_all(
        (
            CalendarEventMirror(
                external_id="visible-google",
                calendar_source=CalendarSource.GOOGLE,
                connection_generation=google_generation,
                summary="Visible",
                start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
                end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
                is_all_day=False,
                created_at=NOW,
                updated_at=NOW,
            ),
            CalendarEventMirror(
                external_id="stale-google",
                calendar_source=CalendarSource.GOOGLE,
                connection_generation=stale_google_generation,
                summary="Stale generation",
                start_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
                is_all_day=False,
                created_at=NOW,
                updated_at=NOW,
            ),
            CalendarEventMirror(
                external_id="unsynced-caldav",
                calendar_source=CalendarSource.CALDAV,
                connection_generation=caldav_generation,
                summary="Unsynced current generation",
                start_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
                end_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
                is_all_day=False,
                created_at=NOW,
                updated_at=NOW,
            ),
        )
    )
    session.flush()

    def resolve_sources():
        return tuple(
            sorted(connected, key=lambda source: source.value)
        )

    def resolve_generation(source):
        return generations.get(source)

    provider = CalendarContextProvider(
        sync_health_store=health,
        source_resolver=resolve_sources,
        account_generation_resolver=resolve_generation,
    )
    layer = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
        calendar_source_resolver=resolve_sources,
        calendar_account_generation_resolver=resolve_generation,
        calendar_sync_health_store=health,
    )
    turn = layer.start_turn(
        _request(),
        policy=_policy(domain="calendar"),
    )
    query = ContextQuery(
        provider_id="calendar",
        capability="calendar.day-summary",
        start=DAY_START,
        end=DAY_END,
    )

    result = await turn.query(session, query)

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["event_count"] == 1
    assert len(result.source_refs) == 1
    source_ref = result.source_refs[0]
    assert source_ref.record_id == calendar_aggregate_identity(
        capability=query.capability,
        start=DAY_START,
        end=DAY_END,
        timezone="UTC",
        granularity="summary",
        parameters={},
        source_scope={
            CalendarSource.GOOGLE.value: google_generation,
        },
    )
    checked, reasons = turn.revalidate_source_ref(
        session,
        query,
        source_ref,
        context_source_refs=(source_ref,),
        now=NOW,
    )
    assert checked is not None
    assert reasons == ()

    health.record_success(
        CalendarSource.CALDAV,
        NOW,
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=caldav_generation,
    )
    checked, reasons = turn.revalidate_source_ref(
        session,
        query,
        source_ref,
        context_source_refs=(source_ref,),
        now=NOW,
    )
    assert checked is None
    assert reasons == ("source_ref_identity_mismatch",)

    generations[CalendarSource.GOOGLE] = stale_google_generation
    checked, reasons = turn.revalidate_source_ref(
        session,
        query,
        source_ref,
        context_source_refs=(source_ref,),
        now=NOW,
    )
    assert checked is None
    assert reasons == ("source_ref_identity_mismatch",)

    generations[CalendarSource.GOOGLE] = google_generation
    connected.remove(CalendarSource.GOOGLE)
    checked, reasons = turn.revalidate_source_ref(
        session,
        query,
        source_ref,
        context_source_refs=(source_ref,),
        now=NOW,
    )
    assert checked is None
    assert reasons == ("source_ref_identity_mismatch",)


def test_access_policy_requires_explicit_grants_and_owner():
    with pytest.raises(ValueError):
        ContextAccessPolicy(
            owner_principal_id=" ",
            grants=(),
        )
    with pytest.raises(ValueError):
        ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(
                DomainAccessGrant(domain="mood"),
                DomainAccessGrant(domain="mood"),
            ),
        )
