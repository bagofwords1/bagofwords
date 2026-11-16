"""add query_executions (per-connection query history, app-wide slots, kill)

One row per query BOW sends to a connection. The row is the history entry a
connection manager reads, the slot behind the per-connection concurrency limit
(counted across every worker, so the limit is app-wide instead of per process),
and the target of a manager's kill request. See
app/data_sources/query_executions.py.

Revision ID: queryexec01
Revises: dashdesc01
Create Date: 2026-11-16
"""
import sqlalchemy as sa

from alembic import op

revision = "queryexec01"
down_revision = "dashdesc01"
branch_labels = None
depends_on = None


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table("query_executions")


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped.
    if _has_table():
        return
    op.create_table(
        "query_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("data_source_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("completion_id", sa.String(length=36), nullable=True),
        sa.Column("agent_execution_id", sa.String(length=36), nullable=True),
        sa.Column("tool_execution_id", sa.String(length=36), nullable=True),
        sa.Column("step_id", sa.String(length=36), nullable=True),
        sa.Column("query_id", sa.String(length=36), nullable=True),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("client_type", sa.String(length=64), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("query_truncated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("queued_ms", sa.Float(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_outcome", sa.String(length=255), nullable=True),
        sa.Column("tool_attempt", sa.Integer(), nullable=True),
        sa.Column("code_attempt", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("killable", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("kill_requested_at", sa.DateTime(), nullable=True),
        sa.Column("killed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_query_executions_id"), "query_executions", ["id"], unique=True)
    op.create_index("ix_query_executions_organization_id", "query_executions", ["organization_id"])
    op.create_index("ix_query_executions_conn_status", "query_executions", ["connection_id", "status"])
    op.create_index("ix_query_executions_conn_queued", "query_executions", ["connection_id", "queued_at"])
    op.create_index("ix_query_executions_created", "query_executions", ["created_at"])


def downgrade() -> None:
    if not _has_table():
        return
    op.drop_index("ix_query_executions_created", table_name="query_executions")
    op.drop_index("ix_query_executions_conn_queued", table_name="query_executions")
    op.drop_index("ix_query_executions_conn_status", table_name="query_executions")
    op.drop_index("ix_query_executions_organization_id", table_name="query_executions")
    op.drop_index(op.f("ix_query_executions_id"), table_name="query_executions")
    op.drop_table("query_executions")
