"""Artifact activity log

Revision ID: activity01
Revises: hostapp01
Create Date: 2026-10-29

* ``artifact_activity_events``: what happened in an app while its editor or
  BOW's own preview used it (requests, stream ends, filters, errors, clicks),
  read by the agent. Short retention (app/services/artifact_activity.py).

Additive only, and idempotent (the dashboard test corpus applies it unstamped,
tests/fixtures/dashboard_corpus/alembic_at.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "activity01"
down_revision: str | None = "hostapp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)
TABLE = "artifact_activity_events"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=True),
        sa.Column("report_id", S36, nullable=True),
        sa.Column("user_id", S36, nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="editor"),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("op", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("shown_in_completion_id", S36, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_activity_events"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                name="fk_artifact_activity_events_organization_id"),
    )
    op.create_index("ix_artifact_activity_events_id", TABLE, ["id"], unique=True)
    op.create_index("ix_artifact_activity_events_organization_id", TABLE, ["organization_id"])
    op.create_index("ix_artifact_activity_report_created", TABLE, ["report_id", "created_at"])
    op.create_index("ix_artifact_activity_artifact_created", TABLE, ["artifact_id", "created_at"])
    op.create_index("ix_artifact_activity_created", TABLE, ["created_at"])


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    for name in ("ix_artifact_activity_created", "ix_artifact_activity_artifact_created",
                 "ix_artifact_activity_report_created", "ix_artifact_activity_events_organization_id",
                 "ix_artifact_activity_events_id"):
        op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
