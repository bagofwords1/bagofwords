"""A dashboard's refresh history

Revision ID: refhist01
Revises: collab01
Create Date: 2026-10-25

`dashboard_refresh_runs`: one row per refresh of a dashboard that ran --
the published release's or the draft's -- and per scheduled run that could
not run, with what started it, how it went, whose credentials it ran with
and the first error. Additive only: no existing row changes, nothing is
backfilled (the history starts with this release).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "refhist01"
down_revision: str | None = "collab01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # Idempotent, as collab01: the dashboard corpus stops before dashfc08's
    # data migration and applies this schema-only revision on its own; a
    # later `upgrade head` then finds it done.
    if _has_table("dashboard_refresh_runs"):
        return
    op.create_table(
        "dashboard_refresh_runs",
        sa.Column("id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("release_id", S36, nullable=True),
        sa.Column("report_id", S36, nullable=True),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("steps_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("steps_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_source", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("run_as_user_id", S36, nullable=True),
        sa.Column("triggered_by_user_id", S36, nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_dashboard_refresh_runs"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], name="fk_dashboard_refresh_runs_artifact_id",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                name="fk_dashboard_refresh_runs_organization_id"),
        sa.ForeignKeyConstraint(["run_as_user_id"], ["users.id"], name="fk_dashboard_refresh_runs_run_as_user_id",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["users.id"],
                                name="fk_dashboard_refresh_runs_triggered_by_user_id", ondelete="SET NULL"),
    )
    op.create_index("ix_dashboard_refresh_runs_artifact_started", "dashboard_refresh_runs",
                    ["artifact_id", "started_at"])


def downgrade() -> None:
    if not _has_table("dashboard_refresh_runs"):
        return
    op.drop_index("ix_dashboard_refresh_runs_artifact_started", table_name="dashboard_refresh_runs")
    op.drop_table("dashboard_refresh_runs")
