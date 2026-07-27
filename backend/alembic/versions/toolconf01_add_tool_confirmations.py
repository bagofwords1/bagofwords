"""add tool_confirmations

Persists mid-run tool approvals ('ask' policy) so any uvicorn worker can
resolve one. Before this, pending approvals lived in a per-process dict, so the
approval POST 404'd whenever it was routed to a worker other than the one
running the completion — the buttons did nothing and the run hung until its
240s timeout.

Revision ID: toolconf01
Revises: entraprof01
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = "toolconf01"
down_revision = "entraprof01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_confirmations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("system_completion_id", sa.String(length=36), nullable=True),
        sa.Column("head_completion_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column("connection_tool_id", sa.String(length=36), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("arguments", sa.JSON(), nullable=True),
        sa.Column("remember", sa.Boolean(), nullable=False),
        sa.Column("resolved_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_confirmations_id", "tool_confirmations", ["id"], unique=True)
    op.create_index("ix_tool_confirmations_status", "tool_confirmations", ["status"])
    op.create_index(
        "ix_tool_confirmations_status_expires", "tool_confirmations", ["status", "expires_at"]
    )
    op.create_index(
        "ix_tool_confirmations_organization_id", "tool_confirmations", ["organization_id"]
    )
    op.create_index("ix_tool_confirmations_report_id", "tool_confirmations", ["report_id"])
    op.create_index(
        "ix_tool_confirmations_system_completion_id", "tool_confirmations", ["system_completion_id"]
    )
    op.create_index(
        "ix_tool_confirmations_head_completion_id", "tool_confirmations", ["head_completion_id"]
    )
    op.create_index("ix_tool_confirmations_user_id", "tool_confirmations", ["user_id"])
    op.create_index(
        "ix_tool_confirmations_connection_tool_id", "tool_confirmations", ["connection_tool_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_tool_confirmations_connection_tool_id", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_user_id", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_head_completion_id", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_system_completion_id", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_report_id", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_organization_id", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_status_expires", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_status", table_name="tool_confirmations")
    op.drop_index("ix_tool_confirmations_id", table_name="tool_confirmations")
    op.drop_table("tool_confirmations")
