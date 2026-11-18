"""Permission mode for tool calls

- reports.permission_mode: the run-level mode picked in the prompt box
  (auto | ask | allow_all). NULL = the organization default.
- data_sources.max_permission_mode: an agent's cap on that mode (ask | auto).
  NULL = no cap.
- tool_confirmations.scope: how far a user's answer reaches
  (once | report | always). NULL on rows written before this column = once,
  or always when ``remember`` is set.

All nullable, no backfill.

Revision ID: permmode01
Revises: worldai01
Create Date: 2026-11-17
"""
import sqlalchemy as sa

from alembic import op

revision = "permmode01"
down_revision = "worldai01"
branch_labels = None
depends_on = None


COLUMNS = (
    ("reports", "permission_mode"),
    ("data_sources", "max_permission_mode"),
    ("tool_confirmations", "scope"),
)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped, so a later
    # `upgrade head` runs it again.
    for table, column in COLUMNS:
        if _has_column(table, column):
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column(column, sa.String(length=16), nullable=True))


def downgrade() -> None:
    for table, column in reversed(COLUMNS):
        if not _has_column(table, column):
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column(column)
