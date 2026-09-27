"""add agent lists (typed per-agent record collections)

Lists are named schemas owned by an agent; the agent fills them through a
natively-registered submit_<slug> tool. Rows accumulate across reports with
per-field evidence and provenance; revisions record every change.

Revision ID: agentlists01
Revises: artchatmodel01
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa


revision = "agentlists01"
down_revision = "artchatmodel01"
branch_labels = None
depends_on = None


def _base_cols():
    return [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "agent_lists",
        *_base_cols(),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("data_source_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("key_field_id", sa.String(length=64), nullable=True),
        sa.Column("require_evidence", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["data_source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_lists_id", "agent_lists", ["id"], unique=True)
    op.create_index("ix_agent_lists_organization_id", "agent_lists", ["organization_id"])
    op.create_index("ix_agent_lists_data_source_id", "agent_lists", ["data_source_id"])

    op.create_table(
        "agent_list_rows",
        *_base_cols(),
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("key_value", sa.String(length=512), nullable=True),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("locked_fields", sa.JSON(), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("tool_execution_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["list_id"], ["agent_lists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_list_rows_id", "agent_list_rows", ["id"], unique=True)
    op.create_index("ix_agent_list_rows_list_id", "agent_list_rows", ["list_id"])
    op.create_index("ix_agent_list_rows_report_id", "agent_list_rows", ["report_id"])
    op.create_index("ix_agent_list_rows_list_key", "agent_list_rows", ["list_id", "key_value"], unique=True)

    op.create_table(
        "agent_list_row_revisions",
        *_base_cols(),
        sa.Column("row_id", sa.String(length=36), nullable=False),
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("tool_execution_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="update"),
        sa.Column("changed", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["row_id"], ["agent_list_rows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["list_id"], ["agent_lists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_list_row_revisions_id", "agent_list_row_revisions", ["id"], unique=True)
    op.create_index("ix_agent_list_row_revisions_row_id", "agent_list_row_revisions", ["row_id"])
    op.create_index("ix_agent_list_row_revisions_list_id", "agent_list_row_revisions", ["list_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_list_row_revisions_list_id", table_name="agent_list_row_revisions")
    op.drop_index("ix_agent_list_row_revisions_row_id", table_name="agent_list_row_revisions")
    op.drop_index("ix_agent_list_row_revisions_id", table_name="agent_list_row_revisions")
    op.drop_table("agent_list_row_revisions")
    op.drop_index("ix_agent_list_rows_list_key", table_name="agent_list_rows")
    op.drop_index("ix_agent_list_rows_report_id", table_name="agent_list_rows")
    op.drop_index("ix_agent_list_rows_list_id", table_name="agent_list_rows")
    op.drop_index("ix_agent_list_rows_id", table_name="agent_list_rows")
    op.drop_table("agent_list_rows")
    op.drop_index("ix_agent_lists_data_source_id", table_name="agent_lists")
    op.drop_index("ix_agent_lists_organization_id", table_name="agent_lists")
    op.drop_index("ix_agent_lists_id", table_name="agent_lists")
    op.drop_table("agent_lists")
