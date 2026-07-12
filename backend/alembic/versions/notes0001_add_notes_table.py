"""add notes table (per-report agent scratchpad)

Notes are the agent's per-report working memory (create_note / edit_note).
Freeform markdown scoped to a report, injected into the planner and knowledge
harness and shown in the report UI. Gated by the enable_agent_notes org setting.

Revision ID: notes0001
Revises: dsicon0001
Create Date: 2026-07-11
"""
from alembic import op
import sqlalchemy as sa


revision = "notes0001"
down_revision = "dsicon0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("agent_execution_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="agent"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"]),
        sa.ForeignKeyConstraint(["agent_execution_id"], ["agent_executions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notes_id", "notes", ["id"], unique=True)
    op.create_index("ix_notes_report_id", "notes", ["report_id"], unique=False)
    op.create_index("ix_notes_agent_execution_id", "notes", ["agent_execution_id"], unique=False)
    op.create_index("ix_notes_organization_id", "notes", ["organization_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_notes_organization_id", table_name="notes")
    op.drop_index("ix_notes_agent_execution_id", table_name="notes")
    op.drop_index("ix_notes_report_id", table_name="notes")
    op.drop_index("ix_notes_id", table_name="notes")
    op.drop_table("notes")
