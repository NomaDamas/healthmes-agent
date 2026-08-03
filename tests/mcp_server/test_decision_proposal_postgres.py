from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastmcp.exceptions import ToolError
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from healthmes.mcp_server import server as server_module
from healthmes.store import Base, DecisionKind, ProposalStatus, create_db_engine
from healthmes.store.models import DecisionRecord, ScheduleProposal, Task

TREE = {"type": "rule", "label": "concurrent proposal claim"}


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_concurrent_decisions_cannot_reassign_the_same_proposal() -> None:
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
    update_gate = threading.Barrier(2, timeout=5)

    with factory() as session:
        task = Task(title="Concurrent claim")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
            proposed_end=datetime(2026, 8, 3, 9, 0, tzinfo=UTC) + timedelta(hours=1),
            status=ProposalStatus.PROPOSED,
        )
        session.add(proposal)
        session.commit()
        proposal_id = proposal.id

    @event.listens_for(engine, "before_cursor_execute")
    def align_claim_updates(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if (
            statement.lstrip().upper().startswith("UPDATE")
            and "schedule_proposal" in statement
            and "decision_record_id" in statement
        ):
            update_gate.wait()

    def claim_once(index: int) -> uuid.UUID | None:
        with factory() as session:
            decision = DecisionRecord(
                kind=DecisionKind.ALERT,
                tree=TREE,
                summary=f"decision {index}",
            )
            session.add(decision)
            session.flush()
            try:
                server_module._claim_schedule_proposals(
                    session,
                    [(str(proposal_id), proposal_id)],
                    decision.id,
                )
                session.commit()
                return decision.id
            except ToolError:
                session.rollback()
                return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [
                future.result(timeout=10)
                for future in (pool.submit(claim_once, 1), pool.submit(claim_once, 2))
            ]

        [winner] = [value for value in winners if value is not None]
        assert winners.count(None) == 1
        with factory() as session:
            stored = session.get(ScheduleProposal, proposal_id)
            assert stored is not None
            assert stored.decision_record_id == winner
    finally:
        event.remove(engine, "before_cursor_execute", align_claim_updates)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
