"""add tool_confirmations.tool_execution_id

A pending approval is found again after a page reload (or from a second tab)
by matching it to the still-running tool execution. That match used the tool
name, but an MCP / Custom API / SSH confirmation stores the inner tool name
(``create_note``) while the execution row stores the dispatched name
(``mcp__<conn>__create_note`` / ``execute_mcp``) — so nothing matched and the
approval card vanished. Store the execution id and match on it.

Nullable: rows written before this revision keep the name-based fallback.

Revision ID: toolconfte01
Revises: queryexec03
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa


revision = "toolconfte01"
down_revision = "queryexec03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tool_confirmations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tool_execution_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            "ix_tool_confirmations_tool_execution_id", ["tool_execution_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_confirmations", schema=None) as batch_op:
        batch_op.drop_index("ix_tool_confirmations_tool_execution_id")
        batch_op.drop_column("tool_execution_id")
