"""Dashboards as first-class entities: a fork's source dashboard

Revision ID: dashfc07
Revises: dashfc06
Create Date: 2026-10-18

Phase 8 of docs/design/dashboards-first-class.md. A fork from /d is a new
conversation; `reports.forked_from_id` stays empty for it (it would name
the owner's conversation, which the forker may not see and which may be
gone). `forked_from_artifact_id` names the dashboard it was made from, so
the editor shows the conversation Fork's banner ("Forked from ...", a link
to /d) and waits for its hydration the same way.

The downgrade drops the column.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc07"
down_revision: str | None = "dashfc06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("forked_from_artifact_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key("fk_reports_forked_from_artifact_id", "artifacts",
                                 ["forked_from_artifact_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_reports_forked_from_artifact_id", "reports", ["forked_from_artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_reports_forked_from_artifact_id", table_name="reports")
    with op.batch_alter_table("reports") as batch:
        batch.drop_constraint("fk_reports_forked_from_artifact_id", type_="foreignkey")
        batch.drop_column("forked_from_artifact_id")
