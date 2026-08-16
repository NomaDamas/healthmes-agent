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
from healthmes.decision.agent import AccessPolicyResolver
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
from healthmes.decision.hermes_profile import (
    HermesDecisionProfileAssertion,
)
from healthmes.decision.policy import (
    DatabaseDecisionPolicyResolver,
)
from healthmes.decision.providers import ContextProviderRegistry
from healthmes.decision.responses import (
    HermesHttpResponsesTransport,
    HermesResponsesDecisionAgent,
    HermesResponsesTransport,
    HermesRuntimeAttestationAssertion,
)
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


def build_healthmes_responses_decision_engine(
    *,
    transport: HermesResponsesTransport,
    search_service: DecisionContextSearchSessionService,
    session_factory: sessionmaker[Session],
    policy_resolver: AccessPolicyResolver,
    model: str,
    provider: str,
    timeout_seconds: float = 60,
    cleanup_timeout_seconds: float = 5,
    session_ttl_seconds: float = 900,
    session_purge_interval_seconds: float = 60,
    finalization_timeout_seconds: float = 5,
    max_pending_requests: int = 8,
    profile_assertion: HermesDecisionProfileAssertion | None = None,
    owns_search_service: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> HealthMesDecisionEngine:
    """Build the single production reasoning path around Hermes Responses."""

    if not callable(policy_resolver):
        raise TypeError("policy_resolver must be callable")

    try:
        agent = HermesResponsesDecisionAgent(
            transport=transport,
            search_service=search_service,
            model=model,
            provider=provider,
            timeout_seconds=timeout_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
            session_ttl_seconds=session_ttl_seconds,
            session_purge_interval_seconds=(
                session_purge_interval_seconds
            ),
            profile_assertion=profile_assertion,
            owns_search_service=owns_search_service,
            clock=clock,
        )
    except BaseException:
        if owns_search_service:
            search_service.close()
        raise

    try:
        finalizer = DecisionFinalizer(
            access_layer=search_service.access_layer,
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
                timeout_seconds
                + cleanup_timeout_seconds
                + finalization_timeout_seconds
                + 1
            ),
        )
    except BaseException:
        try:
            agent.close()
        except Exception:
            _LOGGER.exception(
                "failed to close Responses Decision Agent after "
                "composition failure"
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
        if (
            hostname is None
            or (
                not is_loopback_host(hostname)
                and not settings.decision_hermes_allow_attested_private_http
            )
        ):
            raise ValueError(
                "local decision execution requires a loopback or explicitly "
                "attested private Hermes origin; configure "
                "HEALTHMES_DECISION_EXECUTION_SCOPE=hosted for remote or "
                "cloud processing"
            )
    return scope


def build_decision_context_search_session_service(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    wearable_reader: WearableReader | None = None,
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
        wearable_reader=wearable_reader,
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
    transport: HermesResponsesTransport | None = None,
    search_service: DecisionContextSearchSessionService | None = None,
    wearable_reader: WearableReader | None = None,
    clock: Callable[[], datetime] | None = None,
) -> HealthMesDecisionEngine | None:
    """Compose production decisions through one Hermes Responses turn."""

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
    profile_path = settings.decision_hermes_profile_path
    manifest_path = settings.decision_hermes_runtime_manifest_path
    attestation_key_path = settings.decision_hermes_attestation_key_path
    api_key = settings.decision_hermes_api_key.get_secret_value().strip()
    if transport is None:
        missing_artifacts = [
            name
            for name, value in (
                ("decision_hermes_profile_path", profile_path),
                (
                    "decision_hermes_runtime_manifest_path",
                    manifest_path,
                ),
                (
                    "decision_hermes_attestation_key_path",
                    attestation_key_path,
                ),
            )
            if value is None
        ]
        if missing_artifacts:
            raise ValueError(
                ", ".join(missing_artifacts)
                + " are required for the production Hermes Responses runtime"
            )
    profile_assertion = (
        HermesDecisionProfileAssertion(
            profile_path,
            expected_model=model,
            expected_provider=provider,
            expected_api_key=api_key,
        )
        if profile_path is not None
        else None
    )
    selected_transport = transport
    if selected_transport is None:
        if not api_key:
            raise ValueError(
                "decision_hermes_api_key is required for the production "
                "Hermes Responses runtime"
            )
        if (
            profile_assertion is None
            or manifest_path is None
            or attestation_key_path is None
        ):
            raise AssertionError("decision runtime artifacts were not checked")
        runtime_attestation = HermesRuntimeAttestationAssertion(
            manifest_path=manifest_path,
            attestation_key_path=attestation_key_path,
            profile_assertion=profile_assertion,
            expected_origin=base_url,
            expected_model=model,
            expected_provider=provider,
            expected_api_key=api_key,
        )
        selected_transport = HermesHttpResponsesTransport(
            base_url=base_url,
            api_key=api_key,
            discovery_timeout_seconds=(
                settings.decision_hermes_discovery_timeout_seconds
            ),
            max_response_timeout_seconds=(
                settings.decision_hermes_max_iteration_timeout_seconds
            ),
            runtime_attestation=runtime_attestation,
            allow_attested_private_http=(
                settings.decision_hermes_allow_attested_private_http
            ),
        )
    policy_resolver = DatabaseDecisionPolicyResolver(
        session_factory=session_factory,
        owner_principal_id=settings.decision_owner_principal_id,
        execution_scope=execution_scope,
    )

    owns_search_service = search_service is None
    selected_search_service = search_service
    if selected_search_service is None:
        selected_search_service = (
            build_decision_context_search_session_service(
                settings=settings,
                session_factory=session_factory,
                wearable_reader=wearable_reader,
                clock=clock,
            )
        )

    return build_healthmes_responses_decision_engine(
        transport=selected_transport,
        search_service=selected_search_service,
        session_factory=session_factory,
        policy_resolver=policy_resolver,
        model=model,
        provider=provider,
        timeout_seconds=settings.decision_timeout_seconds,
        cleanup_timeout_seconds=(
            settings.decision_hermes_discovery_timeout_seconds
        ),
        session_ttl_seconds=(
            settings.decision_hermes_session_ttl_seconds
        ),
        session_purge_interval_seconds=(
            settings.decision_hermes_session_purge_interval_seconds
        ),
        finalization_timeout_seconds=(
            settings.decision_finalization_timeout_seconds
        ),
        max_pending_requests=settings.decision_max_pending_requests,
        profile_assertion=profile_assertion,
        owns_search_service=owns_search_service,
        clock=clock,
    )
