"""add officejs_pending_results

Persists pending Office.js tool calls (write_officejs_code, read_excel_range,
read_excel_as_csv, write_to_excel) so any uvicorn worker can resolve the
taskpane's result POST. Before this, pending calls lived in a per-process dict,
so POST /tool-results/{id} 404'd whenever it was routed to a worker other than
the one running the completion — the tool hung until its 55s timeout. The row
also binds the pending call to the initiating user and completion so no other
org member can forge a result.

Revision ID: officejs01
Revises: artchat01
Create Date: 2026-09-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "officejs01"
down_revision: Union[str, None] = "artchat01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "officejs_pending_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("completion_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["completion_id"], ["completions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_officejs_pending_results_id", "officejs_pending_results", ["id"], unique=True
    )
    op.create_index(
        "ix_officejs_pending_results_tool_call_id",
        "officejs_pending_results",
        ["tool_call_id"],
        unique=True,
    )
    op.create_index(
        "ix_officejs_pending_results_status", "officejs_pending_results", ["status"]
    )
    op.create_index(
        "ix_officejs_pending_results_completion_id",
        "officejs_pending_results",
        ["completion_id"],
    )
    op.create_index(
        "ix_officejs_pending_results_user_id", "officejs_pending_results", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_officejs_pending_results_user_id", "officejs_pending_results")
    op.drop_index("ix_officejs_pending_results_completion_id", "officejs_pending_results")
    op.drop_index("ix_officejs_pending_results_status", "officejs_pending_results")
    op.drop_index("ix_officejs_pending_results_tool_call_id", "officejs_pending_results")
    op.drop_index("ix_officejs_pending_results_id", "officejs_pending_results")
    op.drop_table("officejs_pending_results")
