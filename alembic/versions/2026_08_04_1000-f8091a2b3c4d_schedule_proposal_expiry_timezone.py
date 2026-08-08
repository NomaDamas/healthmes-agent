from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "f8091a2b3c4d"
down_revision: str | None = "e7f8091a2b3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    if context.is_offline_mode():
        _alter_expiry_type(timezone=True)
        return
    column = _expires_at_column()
    if not getattr(column["type"], "timezone", False):
        _alter_expiry_type(timezone=True, existing_type=column["type"])


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    if context.is_offline_mode():
        _alter_expiry_type(timezone=False)
        return
    column = _expires_at_column()
    if getattr(column["type"], "timezone", False):
        _alter_expiry_type(timezone=False, existing_type=column["type"])


def _expires_at_column() -> dict[str, object]:
    columns = {
        item["name"]: item
        for item in sa.inspect(op.get_bind()).get_columns("schedule_proposal")
    }
    return columns["expires_at"]


def _alter_expiry_type(
    *,
    timezone: bool,
    existing_type: sa.types.TypeEngine | None = None,
) -> None:
    # The application has always interpreted this legacy naive column as UTC.
    # Preserve that contract independently of the migration session timezone.
    op.alter_column(
        "schedule_proposal",
        "expires_at",
        existing_type=existing_type or sa.DateTime(timezone=not timezone),
        type_=sa.DateTime(timezone=timezone),
        postgresql_using="expires_at AT TIME ZONE 'UTC'",
    )
