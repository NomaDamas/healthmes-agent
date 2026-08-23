"""Database-backed owner consent for Decision Agent context domains."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision.access import (
    ContextAccessPolicy,
    DomainAccessGrant,
)
from healthmes.decision.contracts import (
    DecisionRequest,
    ExecutionScope,
    PrivacyLevel,
)
from healthmes.store import DecisionDomainPolicy

DECISION_DOMAINS = (
    "activity",
    "nutrition",
    "wearable",
    "calendar",
)


def _owner_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("decision owner principal ID must not be blank")
    if len(cleaned) > 255:
        raise ValueError(
            "decision owner principal ID must be at most 255 characters"
        )
    return cleaned


def _domain(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized not in DECISION_DOMAINS:
        raise ValueError(
            f"decision domain must be one of {DECISION_DOMAINS!r}"
        )
    return normalized


def ensure_decision_domain_policies(
    session: Session,
    owner_principal_id: str,
) -> tuple[DecisionDomainPolicy, ...]:
    """Create only missing defaults; existing owner choices are preserved."""

    owner = _owner_id(owner_principal_id)
    existing = {
        row.domain: row
        for row in session.scalars(
            select(DecisionDomainPolicy).where(
                DecisionDomainPolicy.owner_principal_id == owner
            )
        )
        if row.domain in DECISION_DOMAINS
    }
    for domain in DECISION_DOMAINS:
        if domain not in existing:
            row = DecisionDomainPolicy(
                owner_principal_id=owner,
                domain=domain,
                enabled=True,
                revision=1,
            )
            session.add(row)
            existing[domain] = row
    session.flush()
    return tuple(existing[domain] for domain in DECISION_DOMAINS)


def list_decision_domain_policies(
    session: Session,
    owner_principal_id: str,
    *,
    lock: bool = False,
) -> tuple[DecisionDomainPolicy, ...]:
    """Return all known domains, treating missing rows as denied."""

    owner = _owner_id(owner_principal_id)
    statement = select(DecisionDomainPolicy).where(
        DecisionDomainPolicy.owner_principal_id == owner,
        DecisionDomainPolicy.domain.in_(DECISION_DOMAINS),
    )
    if lock:
        statement = statement.with_for_update()
    rows = {
        row.domain: row
        for row in session.scalars(statement)
        if row.domain in DECISION_DOMAINS
    }
    return tuple(
        rows[domain]
        for domain in DECISION_DOMAINS
        if domain in rows
    )


def update_decision_domain_policy(
    session: Session,
    owner_principal_id: str,
    domain: str,
    *,
    enabled: bool,
) -> DecisionDomainPolicy:
    """Update one consent row while preserving the fixed domain vocabulary."""

    owner = _owner_id(owner_principal_id)
    normalized_domain = _domain(domain)
    row = session.scalar(
        select(DecisionDomainPolicy)
        .where(
            DecisionDomainPolicy.owner_principal_id == owner,
            DecisionDomainPolicy.domain == normalized_domain,
        )
        .with_for_update()
    )
    if row is None:
        row = DecisionDomainPolicy(
            owner_principal_id=owner,
            domain=normalized_domain,
            enabled=enabled,
            revision=1,
        )
        session.add(row)
    elif row.enabled != enabled:
        row.enabled = enabled
        row.revision += 1
    session.flush()
    return row


def decision_access_policy(
    *,
    owner_principal_id: str,
    execution_scope: ExecutionScope,
    policies: Sequence[DecisionDomainPolicy],
) -> ContextAccessPolicy:
    """Translate persisted consent switches into the gateway contract."""

    owner = _owner_id(owner_principal_id)
    rows_by_domain = {
        row.domain: row
        for row in policies
        if row.owner_principal_id == owner
        and row.domain in DECISION_DOMAINS
    }
    def grant_for(domain: str) -> DomainAccessGrant:
        row = rows_by_domain.get(domain)
        return DomainAccessGrant(
            domain=domain,
            enabled=bool(row.enabled) if row is not None else False,
            max_privacy_level=PrivacyLevel.AGGREGATE,
            execution_scopes=(execution_scope,),
            consent_scopes=("personal",),
            allow_hosted_raw=False,
            revision=int(row.revision) if row is not None else 0,
        )

    return ContextAccessPolicy(
        owner_principal_id=owner,
        grants=tuple(grant_for(domain) for domain in DECISION_DOMAINS),
        allow_external_provenance=False,
    )


class DatabaseDecisionPolicyResolver:
    """Resolve current consent for planning and atomic finalization."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        owner_principal_id: str,
        execution_scope: ExecutionScope,
    ) -> None:
        self._session_factory = session_factory
        self._owner_principal_id = _owner_id(owner_principal_id)
        self._execution_scope = execution_scope

    def __call__(
        self,
        _request: DecisionRequest,
    ) -> ContextAccessPolicy:
        with self._session_factory() as session:
            policy = self.resolve_in_session(
                _request,
                session,
                lock=False,
            )
            session.rollback()
            return policy

    def resolve_in_session(
        self,
        _request: DecisionRequest,
        session: Session,
        *,
        lock: bool,
    ) -> ContextAccessPolicy:
        rows = list_decision_domain_policies(
            session,
            self._owner_principal_id,
            lock=lock,
        )
        return decision_access_policy(
            owner_principal_id=self._owner_principal_id,
            execution_scope=self._execution_scope,
            policies=rows,
        )
