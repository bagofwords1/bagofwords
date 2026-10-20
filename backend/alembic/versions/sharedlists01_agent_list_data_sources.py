"""share agent lists across agents

A list can now belong to several agents. ``agent_list_data_sources`` holds the
links; every existing list is linked to the agent it was created on, which
stays recorded as its home agent (``agent_lists.data_source_id``).

Revision ID: sharedlists01
Revises: submsg02
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa


revision = "sharedlists01"
down_revision = "submsg02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_list_data_sources",
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("data_source_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["list_id"], ["agent_lists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["data_source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("list_id", "data_source_id"),
    )
    op.create_index(
        "ix_agent_list_data_sources_data_source_id", "agent_list_data_sources", ["data_source_id"]
    )
    # Soft-deleted lists are linked too, so their home stays consistent if one
    # is ever restored. Lists whose agent is already gone (SQLite does not
    # enforce the CASCADE) are skipped.
    op.execute(
        "INSERT INTO agent_list_data_sources (list_id, data_source_id) "
        "SELECT l.id, l.data_source_id FROM agent_lists l "
        "JOIN data_sources d ON d.id = l.data_source_id"
    )


def downgrade() -> None:
    op.drop_index("ix_agent_list_data_sources_data_source_id", table_name="agent_list_data_sources")
    op.drop_table("agent_list_data_sources")
