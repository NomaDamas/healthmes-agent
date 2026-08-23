"""merge decision evidence and policy normalization branches

Revision ID: a6b7c8d9e0f1
Revises: e5f6a7b8c9d0, f4a5b6c7d8e
Create Date: 2026-08-22 16:00:00
"""

from collections.abc import Sequence

revision: str = "a6b7c8d9e0f1"
down_revision: str | Sequence[str] | None = (
    "f4a5b6c7d8e",
    "e5f6a7b8c9d0",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
