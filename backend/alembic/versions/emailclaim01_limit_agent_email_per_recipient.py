"""Claim each agent email recipient before contacting the mail provider.

Revision ID: emailclaim01
Revises: mrgheads05
"""

import sqlalchemy as sa

from alembic import op

revision = "emailclaim01"
down_revision = "mrgheads05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_delivery_claims",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("agent_execution_id", sa.String(36), sa.ForeignKey("agent_executions.id"), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.UniqueConstraint("agent_execution_id", "recipient", name="uq_email_delivery_run_recipient"),
    )
    op.create_index("ix_email_delivery_claims_agent_execution_id", "email_delivery_claims", ["agent_execution_id"])


def downgrade() -> None:
    op.drop_index("ix_email_delivery_claims_agent_execution_id", table_name="email_delivery_claims")
    op.drop_table("email_delivery_claims")
