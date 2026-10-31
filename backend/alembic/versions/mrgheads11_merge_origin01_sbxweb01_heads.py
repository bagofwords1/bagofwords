"""merge the embed line (origin01, #1514) with the sandbox web-fetch line (sbxweb01, #1520)

Revision ID: mrgheads11
Revises: origin01, sbxweb01
Create Date: 2026-10-30

Both lines start at connfail01. A database that already ran either line gets
the other one on `alembic upgrade head`, with no stamping (origin01 is
additive and idempotent; sbxweb01 is data-only and skips orgs that already
have the key).
"""
from typing import Sequence, Union

revision: str = "mrgheads11"
down_revision: Union[str, None] = ("origin01", "sbxweb01")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
