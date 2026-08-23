from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from healthmes.calendars.adjustments import issue_reply_handle
from healthmes.mcp_server import server as server_module
from healthmes.schedule_proposals import (
    ScheduleProposalResolutionError,
    resolve_schedule_proposal,
)
from healthmes.store import Base, DecisionKind, ProposalStatus, create_db_engine
from healthmes.store.models import DecisionRecord, ScheduleProposal, Task

HANDLE_SECRET = "postgres-proposal-test-secret-at-least-32-characters"


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_bounded_schedule_command_persists_one_internal_decision_record(
    settings,
) -> None:
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
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    active_settings = settings.model_copy(
        update={
            "database_url": database_url,
            "public_base_url": "http://healthmes.test:8100",
            "calendar_adjustment_secret": SecretStr(HANDLE_SECRET),
            "scheduler_enabled": False,
        }
    )
    start = datetime.now(UTC).replace(
        second=0,
        microsecond=0,
    ) + timedelta(days=1)

    try:
        server_module.set_settings(active_settings)
        server_module.set_session_factory(factory)
        server_module.set_timezone(UTC)
        result = server_module.propose_schedule_blocks(
            [
                server_module.ScheduleBlockIn(
                    title="Morning focus",
                    energy_demand="high",
                    start=start.isoformat(),
                    end=(start + timedelta(hours=1)).isoformat(),
                ),
                server_module.ScheduleBlockIn(
                    title="Recovery walk",
                    energy_demand="low",
                    start=(start + timedelta(hours=2)).isoformat(),
                    end=(start + timedelta(hours=3)).isoformat(),
                ),
            ]
        )

        assert result["status"] == "ok"
        assert len(result["proposals"]) == 2
        with factory() as session:
            proposals = list(session.scalars(sa.select(ScheduleProposal)))
            decisions = list(session.scalars(sa.select(DecisionRecord)))

        assert len(proposals) == 2
        assert len(decisions) == 1
        [decision] = decisions
        assert {
            proposal.decision_record_id for proposal in proposals
        } == {decision.id}
        assert str(decision.id) == result["decision_record_id"]
        assert decision.kind is DecisionKind.SCHEDULE_CHANGE
        assert decision.decision_request_id is None
        assert decision.decision_turn_id is None
        assert decision.decision_request_fingerprint is None
        assert decision.decision_payload is None
        assert decision.decision_payload_digest is None
        assert decision.tree["detail"] == {
            "proposal_count": 2,
            "confirmation_required": True,
        }
    finally:
        server_module.reset_runtime_state()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_resolution_rechecks_expiry_after_waiting_for_a_row_lock() -> None:
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
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with factory() as session:
        task = Task(title="Expires while waiting")
        session.add(task)
        session.flush()
        handle = issue_reply_handle(HANDLE_SECRET)
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=datetime.now(UTC) + timedelta(hours=1),
            proposed_end=datetime.now(UTC) + timedelta(hours=2),
            status=ProposalStatus.PROPOSED,
            reply_handle_digest=handle.digest,
            expires_at=datetime.now(UTC) + timedelta(seconds=0.5),
        )
        session.add(proposal)
        session.commit()
        proposal_id = proposal.id

    result: list[str] = []

    def resolve_while_blocked() -> None:
        with factory() as session:
            try:
                resolve_schedule_proposal(
                    session,
                    proposal_id,
                    ProposalStatus.ACCEPTED,
                    handle.plaintext,
                    HANDLE_SECRET,
                )
            except ScheduleProposalResolutionError as exc:
                result.append(exc.code)
            else:
                session.commit()
                result.append("accepted")

    try:
        with factory() as locker:
            locker.scalar(
                sa.select(ScheduleProposal)
                .where(ScheduleProposal.id == proposal_id)
                .with_for_update()
            )
            worker = threading.Thread(target=resolve_while_blocked)
            worker.start()
            time.sleep(0.8)
            locker.commit()
            worker.join(timeout=5)

        assert result == ["expired"]
        with factory() as session:
            stored = session.get(ScheduleProposal, proposal_id)
            assert stored is not None
            assert stored.status is ProposalStatus.PROPOSED
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
