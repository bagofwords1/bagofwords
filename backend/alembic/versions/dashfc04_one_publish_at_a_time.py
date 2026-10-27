"""Dashboards as first-class entities: one publish at a time per dashboard; step references indexed

Revision ID: dashfc04
Revises: dashfc03
Create Date: 2026-10-14

Phase 5 of docs/design/dashboards-first-class.md. Publish creates a release
in status 'publishing', copies the draft into it and runs it in the
background. Two publishes of one dashboard at once would race to replace the
live release, so a dashboard holds at most one release in 'publishing': a
partial unique index, which both SQLite and Postgres enforce, so two servers
cannot both start one. No release is written before this phase, so no
existing row can violate it.

Publishing deletes the previous release's copied steps, and Postgres checks
every column that references steps.id for each deleted row. Four of them had
no index, so each check read the whole table (completions above all):
completions.step_id, tool_executions.created_step_id,
entities.source_step_id, table_feedback_events.step_id. Indexed here. Building
them reads each table once at the upgrade.

The downgrade drops the indexes; no data changes.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc04"
down_revision: str | None = "dashfc03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WHERE = sa.text("status = 'publishing'")
# (index, table, column): the columns that reference steps.id without one.
_STEP_REFERENCES = (
    ("ix_completions_step_id", "completions", "step_id"),
    ("ix_tool_executions_created_step_id", "tool_executions", "created_step_id"),
    ("ix_entities_source_step_id", "entities", "source_step_id"),
    ("ix_table_feedback_events_step_id", "table_feedback_events", "step_id"),
)


def upgrade() -> None:
    op.create_index("uq_artifact_releases_one_publishing", "artifact_releases", ["artifact_id"], unique=True,
                    sqlite_where=_WHERE, postgresql_where=_WHERE)
    for name, table, column in _STEP_REFERENCES:
        op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _ in reversed(_STEP_REFERENCES):
        op.drop_index(name, table_name=table)
    op.drop_index("uq_artifact_releases_one_publishing", table_name="artifact_releases")
