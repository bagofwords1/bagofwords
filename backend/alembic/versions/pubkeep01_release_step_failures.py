"""Publish: which charts could not run, and which kept their previous data

Revision ID: pubkeep01
Revises: atlv201
Create Date: 2026-11-01

Publish re-runs every copied step. It used to stop at the first one that
failed and say only that one. It now runs them all and records every
failure on the release (`step_failures`: [{step, reason, keepable, kept}]):
on a failed attempt, the charts that could not run, so the owner can choose
to publish them with their last data; on a live release, the charts that
did just that. NULL: nothing failed (and every release before this one).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "pubkeep01"
down_revision: str | None = "atlv201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: the dashboard corpus runs it unstamped before `upgrade head`.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("artifact_releases")}
    if "step_failures" in columns:
        return
    with op.batch_alter_table("artifact_releases") as batch:
        batch.add_column(sa.Column("step_failures", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifact_releases") as batch:
        batch.drop_column("step_failures")
