"""add description to artifacts

A short, human-written blurb of what a dashboard shows and who it is for
(dashdesc01). Distinct from `notes` (the long README fed to editing agents):
this never reaches an LLM and is shown to readers on hover, beside the full
name. Owner-editable from the General settings tab. NULL = none.

Revision ID: dashdesc01
Revises: decmodel01
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa


revision = "dashdesc01"
down_revision = "decmodel01"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    return any(c["name"] == "description" for c in sa.inspect(op.get_bind()).get_columns("artifacts"))


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped.
    if _has_column():
        return
    op.add_column("artifacts", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    op.drop_column("artifacts", "description")
