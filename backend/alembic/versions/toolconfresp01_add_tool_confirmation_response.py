"""add tool_confirmations.response

A builtin-tool confirmation can now carry the user's answer beyond
allow/deny — e.g. create_demo_dataset's card returns which suggested agents
stayed ticked, or the feedback typed on reject. Stored on the row (not only
in the in-process future) so a decision posted to another worker reaches the
waiting run intact.

Revision ID: toolconfresp01
Revises: mrgheads06
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa


revision = "toolconfresp01"
down_revision = "mrgheads06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tool_confirmations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("response", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tool_confirmations", schema=None) as batch_op:
        batch_op.drop_column("response")
