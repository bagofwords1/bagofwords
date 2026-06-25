"""add follow_ups to completions (and merge heads)

Persist suggested follow-up questions generated on the small/default model at
the end of a web-session agent run, so the chips survive a page reload. The
live render comes from the in-stream `completion.follow_ups` SSE event; this
column is the durable source of truth.

Also serves as a merge point for the two existing heads (judge0001 and
usravatar01) so the tree collapses back to a single head.

Revision ID: followups01
Revises: judge0001, usravatar01
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa


revision = "followups01"
down_revision = ("judge0001", "usravatar01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("completions", sa.Column("follow_ups", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("completions", "follow_ups")
