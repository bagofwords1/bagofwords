"""Add durable native identity and ordering to tool executions.

Revision ID: durctx01
Revises: rov01

No data backfill is required. Existing executions keep NULL provider fields
and the transcript loader assigns stable synthetic ids from their primary key.
"""

import sqlalchemy as sa

from alembic import op

revision = "durctx01"
down_revision = "rov01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tool_executions") as batch_op:
        batch_op.add_column(sa.Column("provider_call_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provider_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("provider_signature", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("action_index", sa.Integer(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_tool_executions_decision_action",
            ["plan_decision_id", "action_index"],
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_executions") as batch_op:
        batch_op.drop_constraint(
            "uq_tool_executions_decision_action", type_="unique"
        )
        batch_op.drop_column("action_index")
        batch_op.drop_column("provider_signature")
        batch_op.drop_column("provider_name")
        batch_op.drop_column("provider_call_id")
