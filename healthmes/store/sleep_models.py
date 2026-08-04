from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from healthmes.store.base import JSONB, Base, str_255, string_enum
from healthmes.store.enums import CalendarSource, SleepProposalStatus


class SleepReconciliationProposal(Base):
    __tablename__ = "sleep_reconciliation_proposal"
    __table_args__ = (
        sa.UniqueConstraint(
            "dedup_key",
            name="uq_sleep_reconciliation_proposal_dedup_key",
        ),
    )

    calendar_source: Mapped[CalendarSource] = mapped_column(index=True)
    local_date: Mapped[date] = mapped_column(index=True)
    source_key: Mapped[str_255]
    observation_fingerprint: Mapped[str_255]
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    provider_state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[SleepProposalStatus] = mapped_column(
        string_enum(SleepProposalStatus),
        default=SleepProposalStatus.PENDING,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(index=True)
    consumed_at: Mapped[datetime | None]
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dedup_key: Mapped[str_255]
