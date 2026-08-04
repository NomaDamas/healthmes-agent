"""merge calendar adjustment and monthly goal migration heads

Revision ID: a7c9e2f41b6d
Revises: 7dbf6b51f4c8, d5e7f3a1c2b9
Create Date: 2026-07-27 12:00:00.000000

"""

from collections.abc import Sequence

revision: str = "a7c9e2f41b6d"
down_revision: tuple[str, str] | None = ("7dbf6b51f4c8", "d5e7f3a1c2b9")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
