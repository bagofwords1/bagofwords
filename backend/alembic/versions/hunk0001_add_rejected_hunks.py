"""add rejected_hunks to instruction_builds

Per-hunk tracked changes (immutable cherry-pick model): a suggestion build
records which of its hunks the reviewer rejected. Accepts need no state (they
advance main and drop out of the diff); only rejections are persisted here.

Revision ID: hunk0001
Revises: rev1ew0001
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa


revision = "hunk0001"
down_revision = "rev1ew0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instruction_builds",
        sa.Column("rejected_hunks", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instruction_builds", "rejected_hunks")
