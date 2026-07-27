"""add memory column to memberships

Revision ID: usermem01
Revises: ctxcomp01
Create Date: 2026-07-18 00:00:00.000000

Per-user, per-org agent memory (the "MEMORY.md" tier): small, curated,
agent-written durable facts about the user — preferences, writing style,
analyses they liked. Lives on the membership row (not the user row) because,
like ``note``, it is org-scoped: a user's memory in one org must never leak
into another. Distinct from the per-report Notes scratchpad.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "usermem01"
down_revision: Union[str, None] = "ctxcomp01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memberships", sa.Column("memory", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("memberships", "memory")
