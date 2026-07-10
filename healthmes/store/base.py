"""Declarative base and portable column types for the healthmes database.

Conventions follow ``vendor/open-wearables/backend/app/database.py`` (declarative
base with a ``type_annotation_map``) and ``app/mappings.py`` (``Annotated``
column aliases), adapted so every model runs both on postgres (full stack) and
on sqlite (zero-setup mac-native dev and unit tests):

- ``JSONB`` is plain ``JSON`` on sqlite and native ``JSONB`` on postgres.
- ``uuid.UUID`` maps to :class:`sqlalchemy.Uuid` (native UUID on postgres,
  CHAR(32) on sqlite) with a client-side ``uuid4`` default.
- Str-valued enums are stored as plain VARCHAR (``native_enum=False`` — no
  postgres ``CREATE TYPE``), while reads still return enum instances.
"""

import enum
import uuid
from datetime import date, datetime
from typing import Annotated, Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from healthmes.store.enums import (
    CalendarSource,
    DecisionKind,
    EnergyDemand,
    MedicalRecordKind,
    ProposalStatus,
    TaskSource,
)

# Deterministic constraint/index names so Alembic can address them in future
# migrations (classic SQLAlchemy naming convention).
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Portable JSON document type: JSONB on postgres, plain JSON elsewhere
# (sqlite). Mirrors the intent of open-wearables' ``json_binary`` alias
# without pinning models to the postgres dialect.
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> sa.Enum:
    """A VARCHAR-backed enum column type storing the enum *values*.

    ``native_enum=False`` keeps DDL identical on postgres and sqlite (no
    ``CREATE TYPE``); no CHECK constraint is emitted (SQLAlchemy default),
    matching open-wearables' plain-string enum storage while still returning
    enum instances on read.
    """
    return sa.Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


# Annotated column aliases, app/mappings.py style.
str_32 = Annotated[str, mapped_column(sa.String(32))]
str_64 = Annotated[str, mapped_column(sa.String(64))]
str_255 = Annotated[str, mapped_column(sa.String(255))]
JSONDict = Annotated[dict[str, Any], mapped_column(JSONB)]


class Base(DeclarativeBase):
    """Declarative base for all healthmes domain models.

    Every table gets a client-generated UUID primary key plus
    ``created_at``/``updated_at`` server-side timestamps (docs/PLAN.md §2).
    ``sort_order`` keeps ``id`` first and the timestamps last in DDL.
    """

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        str: sa.Text(),
        date: sa.Date(),
        datetime: sa.DateTime(timezone=True),
        uuid.UUID: sa.Uuid(),
        # Domain enums, stored as their string values (VARCHAR on both backends).
        EnergyDemand: string_enum(EnergyDemand),
        TaskSource: string_enum(TaskSource),
        CalendarSource: string_enum(CalendarSource),
        ProposalStatus: string_enum(ProposalStatus),
        DecisionKind: string_enum(DecisionKind),
        MedicalRecordKind: string_enum(MedicalRecordKind),
    }

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4, sort_order=-100)
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now(), sort_order=100)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now(), sort_order=101
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        mapper = sa.inspect(self.__class__)
        fields = [f"{col.key}={getattr(self, col.key, None)!r}" for col in mapper.columns]
        return f"<{self.__class__.__name__}({', '.join(fields)})>"
