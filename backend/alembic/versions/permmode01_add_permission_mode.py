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
Revises: queryexec03
Create Date: 2026-11-17
"""
import sqlalchemy as sa

from alembic import op

revision = "permmode01"
down_revision = "queryexec03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.add_column(sa.Column("permission_mode", sa.String(length=16), nullable=True))
    with op.batch_alter_table("data_sources", schema=None) as batch_op:
        batch_op.add_column(sa.Column("max_permission_mode", sa.String(length=16), nullable=True))
    with op.batch_alter_table("tool_confirmations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tool_confirmations", schema=None) as batch_op:
        batch_op.drop_column("scope")
    with op.batch_alter_table("data_sources", schema=None) as batch_op:
        batch_op.drop_column("max_permission_mode")
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.drop_column("permission_mode")
