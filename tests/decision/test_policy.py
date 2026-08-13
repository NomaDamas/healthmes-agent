from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from healthmes.decision import (
    DECISION_DOMAINS,
    DatabaseDecisionPolicyResolver,
    DecisionCaller,
    DecisionRequest,
    ExecutionScope,
    decision_access_policy,
    ensure_decision_domain_policies,
    list_decision_domain_policies,
    update_decision_domain_policy,
)
from healthmes.store import Base, DecisionDomainPolicy, create_db_engine


@pytest.fixture
def persistence():
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _request(
    execution_scope: ExecutionScope = ExecutionScope.LOCAL,
) -> DecisionRequest:
    return DecisionRequest(
        question="Should I keep working?",
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=execution_scope,
        ),
    )


def test_bootstrap_creates_all_domains_once_and_preserves_owner_choice(
    persistence,
) -> None:
    _engine, factory = persistence
    with factory() as session:
        created = ensure_decision_domain_policies(session, " owner ")
        session.commit()

    assert tuple(row.domain for row in created) == DECISION_DOMAINS
    assert all(row.enabled for row in created)

    with factory() as session:
        update_decision_domain_policy(
            session,
            "owner",
            "nutrition",
            enabled=False,
        )
        session.commit()

    with factory() as session:
        bootstrapped = ensure_decision_domain_policies(session, "owner")
        session.commit()
        count = session.scalar(
            sa.select(sa.func.count()).select_from(DecisionDomainPolicy)
        )

    assert count == len(DECISION_DOMAINS)
    assert {
        row.domain: row.enabled for row in bootstrapped
    } == {
        "activity": True,
        "nutrition": False,
        "wearable": True,
        "calendar": True,
    }


def test_missing_or_foreign_rows_are_denied_in_resolved_policy(
    persistence,
) -> None:
    _engine, factory = persistence
    with factory() as session:
        session.add_all(
            [
                DecisionDomainPolicy(
                    owner_principal_id="owner",
                    domain="activity",
                    enabled=True,
                ),
                DecisionDomainPolicy(
                    owner_principal_id="another-owner",
                    domain="nutrition",
                    enabled=True,
                ),
            ]
        )
        session.commit()
        rows = list_decision_domain_policies(session, "owner")

    policy = decision_access_policy(
        owner_principal_id="owner",
        execution_scope=ExecutionScope.HOSTED,
        policies=rows,
    )

    assert {
        grant.domain: grant.enabled for grant in policy.grants
    } == {
        "activity": True,
        "nutrition": False,
        "wearable": False,
        "calendar": False,
    }
    assert all(
        grant.execution_scopes == (ExecutionScope.HOSTED,)
        for grant in policy.grants
    )


def test_database_resolver_reads_the_latest_persisted_choice(
    persistence,
) -> None:
    _engine, factory = persistence
    with factory() as session:
        ensure_decision_domain_policies(session, "owner")
        session.commit()

    resolver = DatabaseDecisionPolicyResolver(
        session_factory=factory,
        owner_principal_id="owner",
        execution_scope=ExecutionScope.LOCAL,
    )
    initial = resolver(_request())

    with factory() as session:
        update_decision_domain_policy(
            session,
            "owner",
            "calendar",
            enabled=False,
        )
        session.commit()

    updated = resolver(_request())

    assert all(grant.enabled for grant in initial.grants)
    assert {
        grant.domain: grant.enabled for grant in updated.grants
    }["calendar"] is False


@pytest.mark.parametrize(
    ("operation", "match"),
    (
        ("blank_owner", "must not be blank"),
        ("long_owner", "at most 255"),
        ("unknown_domain", "must be one of"),
    ),
)
def test_policy_identifiers_are_strictly_validated(
    persistence,
    operation,
    match,
) -> None:
    _engine, factory = persistence
    with factory() as session, pytest.raises(ValueError, match=match):
        if operation == "blank_owner":
            ensure_decision_domain_policies(session, " ")
        elif operation == "long_owner":
            list_decision_domain_policies(session, "x" * 256)
        else:
            update_decision_domain_policy(
                session,
                "owner",
                "medical",
                enabled=True,
            )
