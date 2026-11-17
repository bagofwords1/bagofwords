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


TABLE = "tool_confirmations"
COLUMN = "tool_execution_id"
INDEX = "ix_tool_confirmations_tool_execution_id"


def _columns() -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _indexes() -> set:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped, so a later
    # `upgrade head` runs it again.
    has_column, has_index = COLUMN in _columns(), INDEX in _indexes()
    if has_column and has_index:
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        if not has_column:
            batch_op.add_column(sa.Column(COLUMN, sa.String(length=36), nullable=True))
        if not has_index:
            batch_op.create_index(INDEX, [COLUMN], unique=False)


def downgrade() -> None:
    has_column, has_index = COLUMN in _columns(), INDEX in _indexes()
    if not has_column and not has_index:
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        if has_index:
            batch_op.drop_index(INDEX)
        if has_column:
            batch_op.drop_column(COLUMN)
