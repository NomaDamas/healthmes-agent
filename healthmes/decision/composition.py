"""Explicit composition roots for HealthMes decision components."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.jobs import connected_sources
from healthmes.calendars.state import FileSyncHealthStore, SyncHealthStore
from healthmes.config import Settings, is_loopback_host
from healthmes.decision.access import (
    ContextAccessLayer,
    ContextAccessPolicy,
    DomainAccessGrant,
)
from healthmes.decision.agent import (
    AccessPolicyResolver,
    HealthMesDecisionAgent,
)
from healthmes.decision.contracts import (
    ExecutionScope,
    PrivacyLevel,
)
from healthmes.decision.domain_providers import (
    ActivityContextProvider,
    CalendarContextProvider,
    CalendarSourceResolver,
    NutritionContextProvider,
    WearableContextProvider,
    WearableReader,
)
from healthmes.decision.engine import HealthMesDecisionEngine
from healthmes.decision.finalizer import DecisionFinalizer
from healthmes.decision.hermes import (
    HermesHttpIterationTransport,
    HermesIterationTransport,
    HermesRuntimeAdapter,
)
from healthmes.decision.policy import (
    DatabaseDecisionPolicyResolver,
)
from healthmes.decision.providers import ContextProviderRegistry
from healthmes.decision.runtime import DecisionRuntime
from healthmes.decision.search import (
    DecisionContextSearchSessionService,
)
from healthmes.store.enums import CalendarSource

_LOGGER = logging.getLogger(__name__)


def build_context_provider_registry(
    *,
    calendar_settings: Settings | None = None,
    wearable_reader: WearableReader | None = None,
    session_factory: sessionmaker[Session] | None = None,
    calendar_sync_health_store: SyncHealthStore | None = None,
    calendar_sources: tuple[CalendarSource, ...] = (),
    calendar_source_resolver: CalendarSourceResolver | None = None,
    calendar_account_generation_resolver: (
        Callable[[CalendarSource], str | None] | None
    ) = None,
) -> ContextProviderRegistry:
    """Build the default registry without import-time global registration."""

    return ContextProviderRegistry(
        (
            ActivityContextProvider(),
            NutritionContextProvider(),
            WearableContextProvider(
                wearable_reader,
                snapshot_session_factory=session_factory,
            ),
            CalendarContextProvider(
                settings=calendar_settings,
                sync_health_store=calendar_sync_health_store,
                sources=calendar_sources,
                source_resolver=calendar_source_resolver,
                account_generation_resolver=(
                    calendar_account_generation_resolver
                ),
            ),
        )
    )


def build_healthmes_decision_engine(
    *,
    runtime: DecisionRuntime,
    session_factory: sessionmaker[Session],
    policy_resolver: AccessPolicyResolver,
    calendar_settings: Settings | None = None,
    wearable_reader: WearableReader | None = None,
    calendar_sync_health_store: SyncHealthStore | None = None,
    calendar_sources: tuple[CalendarSource, ...] = (),
    calendar_source_resolver: CalendarSourceResolver | None = None,
    calendar_account_generation_resolver: (
        Callable[[CalendarSource], str | None] | None
    ) = None,
    timeout_seconds: float = 60,
    finalization_timeout_seconds: float = 5,
    max_pending_requests: int = 8,
    clock: Callable[[], datetime] | None = None,
) -> HealthMesDecisionEngine:
    """Build the production decision flow with one policy and gateway.

    There is deliberately no broad-consent default. The application boundary
    must resolve the authenticated owner's current policy for every request.
    """

    if not callable(policy_resolver):
        raise TypeError("policy_resolver must be callable")

    registry = build_context_provider_registry(
        calendar_settings=calendar_settings,
        wearable_reader=wearable_reader,
        session_factory=session_factory,
        calendar_sync_health_store=calendar_sync_health_store,
        calendar_sources=calendar_sources,
        calendar_source_resolver=calendar_source_resolver,
        calendar_account_generation_resolver=(
            calendar_account_generation_resolver
        ),
    )
    access_layer = ContextAccessLayer(
        registry,
        clock=clock,
        calendar_settings=calendar_settings,
        calendar_sync_health_store=calendar_sync_health_store,
        calendar_source_resolver=calendar_source_resolver,
        calendar_account_generation_resolver=(
            calendar_account_generation_resolver
        ),
    )
    agent = HealthMesDecisionAgent(
        access_layer=access_layer,
        runtime=runtime,
        session_factory=session_factory,
        policy_resolver=policy_resolver,
        timeout_seconds=timeout_seconds,
        clock=clock,
    )
    try:
        finalizer = DecisionFinalizer(
            access_layer=access_layer,
            session_factory=session_factory,
            policy_resolver=policy_resolver,
            timeout_seconds=finalization_timeout_seconds,
            max_workers=max_pending_requests,
            clock=clock,
        )
        return HealthMesDecisionEngine(
            agent=agent,
            finalizer=finalizer,
            max_pending_requests=max_pending_requests,
            shutdown_timeout_seconds=(
                timeout_seconds + finalization_timeout_seconds + 1
            ),
        )
    except BaseException:
        try:
            agent.close()
        except Exception:
            _LOGGER.exception(
                "failed to close Decision Agent after composition failure"
            )
        raise


def local_owner_access_policy(
    owner_principal_id: str,
    *,
    execution_scope: ExecutionScope = ExecutionScope.LOCAL,
) -> ContextAccessPolicy:
    """Compatibility helper; production resolves persisted owner consent."""

    return ContextAccessPolicy(
        owner_principal_id=owner_principal_id,
        grants=tuple(
            DomainAccessGrant(
                domain=domain,
                max_privacy_level=PrivacyLevel.AGGREGATE,
                execution_scopes=(execution_scope,),
                consent_scopes=("personal",),
                allow_hosted_raw=False,
            )
            for domain in (
                "activity",
                "nutrition",
                "wearable",
                "calendar",
            )
        ),
        allow_external_provenance=False,
    )


def resolve_decision_execution_scope(
    settings: Settings,
) -> ExecutionScope:
    """Validate the server-owned runtime location, including model proxies."""

    scope = ExecutionScope(settings.decision_execution_scope)
    base_url = settings.decision_hermes_base_url
    if base_url is not None and scope is ExecutionScope.LOCAL:
        hostname = urlsplit(base_url).hostname
        if hostname is None or not is_loopback_host(hostname):
            raise ValueError(
                "local decision execution requires a loopback Hermes origin; "
                "configure HEALTHMES_DECISION_EXECUTION_SCOPE=hosted for "
                "remote or cloud processing"
            )
    return scope


def build_decision_context_search_session_service(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    clock: Callable[[], datetime] | None = None,
    monotonic_clock: Callable[[], float] | None = None,
) -> DecisionContextSearchSessionService:
    """Compose the read-only MCP search boundary independently of Hermes."""

    execution_scope = resolve_decision_execution_scope(settings)

    def source_resolver() -> tuple[CalendarSource, ...]:
        return connected_sources(settings)

    def account_generation_resolver(
        source: CalendarSource,
    ) -> str | None:
        return creds.calendar_account_generation(settings, source)

    sync_health_store = FileSyncHealthStore.for_data_dir(settings.data_dir)
    registry = build_context_provider_registry(
        calendar_settings=settings,
        wearable_reader=None,
        session_factory=session_factory,
        calendar_sync_health_store=sync_health_store,
        calendar_source_resolver=source_resolver,
        calendar_account_generation_resolver=(
            account_generation_resolver
        ),
    )
    access_layer = ContextAccessLayer(
        registry,
        clock=clock,
        calendar_settings=settings,
        calendar_sync_health_store=sync_health_store,
        calendar_source_resolver=source_resolver,
        calendar_account_generation_resolver=(
            account_generation_resolver
        ),
    )
    policy_resolver = DatabaseDecisionPolicyResolver(
        session_factory=session_factory,
        owner_principal_id=settings.decision_owner_principal_id,
        execution_scope=execution_scope,
    )
    return DecisionContextSearchSessionService(
        access_layer=access_layer,
        session_factory=session_factory,
        policy_resolver=policy_resolver,
        ttl_seconds=settings.decision_timeout_seconds,
        max_active_sessions=settings.decision_max_pending_requests,
        clock=clock,
        monotonic_clock=monotonic_clock,
    )


def build_configured_decision_engine(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    transport: HermesIterationTransport | None = None,
    wearable_reader: WearableReader | None = None,
    clock: Callable[[], datetime] | None = None,
) -> HealthMesDecisionEngine | None:
    """Compose the service decision singleton from an all-or-none setting."""

    base_url = settings.decision_hermes_base_url
    model = settings.decision_hermes_model
    provider = settings.decision_hermes_provider
    if base_url is None or model is None or provider is None:
        if transport is not None:
            raise ValueError(
                "a decision transport requires configured Hermes runtime "
                "identity"
            )
        return None

    execution_scope = resolve_decision_execution_scope(settings)
    selected_transport = transport or HermesHttpIterationTransport(
        base_url=base_url,
        api_key=(
            settings.decision_hermes_api_key.get_secret_value().strip()
            or None
        ),
        discovery_timeout_seconds=(
            settings.decision_hermes_discovery_timeout_seconds
        ),
        max_iteration_timeout_seconds=(
            settings.decision_hermes_max_iteration_timeout_seconds
        ),
    )
    runtime = HermesRuntimeAdapter(
        transport=selected_transport,
        model=model,
        provider=provider,
    )
    policy_resolver = DatabaseDecisionPolicyResolver(
        session_factory=session_factory,
        owner_principal_id=settings.decision_owner_principal_id,
        execution_scope=execution_scope,
    )

    def source_resolver() -> tuple[CalendarSource, ...]:
        return connected_sources(settings)

    def account_generation_resolver(
        source: CalendarSource,
    ) -> str | None:
        return creds.calendar_account_generation(settings, source)

    return build_healthmes_decision_engine(
        runtime=runtime,
        session_factory=session_factory,
        policy_resolver=policy_resolver,
        calendar_settings=settings,
        wearable_reader=wearable_reader,
        calendar_sync_health_store=(
            FileSyncHealthStore.for_data_dir(settings.data_dir)
        ),
        calendar_source_resolver=source_resolver,
        calendar_account_generation_resolver=(
            account_generation_resolver
        ),
        timeout_seconds=settings.decision_timeout_seconds,
        finalization_timeout_seconds=(
            settings.decision_finalization_timeout_seconds
        ),
        max_pending_requests=settings.decision_max_pending_requests,
        clock=clock,
    )
