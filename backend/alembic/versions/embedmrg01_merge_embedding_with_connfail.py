"""merge the embedding branch (hostapp01 → activity01) with main's connfail01

Revision ID: embedmrg01
Revises: activity01, connfail01
Create Date: 2026-10-30

Both lines start at refhist01. A database that already ran either line gets
the other one on `alembic upgrade head`, with no stamping (connfail01,
hostapp01 and activity01 are idempotent).
"""
from typing import Sequence, Union

revision: str = "embedmrg01"
down_revision: Union[str, None] = ("activity01", "connfail01")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
