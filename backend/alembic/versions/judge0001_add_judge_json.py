"""add judge_json to completions

Persist the LLM judge's free-text rationale (and room for future judge
metadata) alongside the existing numeric scores, so the diagnosis trace
modal can explain each score, not just show the number. Kept as a single
JSON column because the shape will evolve; the scalar score columns stay
as-is for queryable diagnosis filters/metrics.

Revision ID: judge0001
Revises: hunk0001
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa


revision = "judge0001"
down_revision = "hunk0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("completions", sa.Column("judge_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("completions", "judge_json")
