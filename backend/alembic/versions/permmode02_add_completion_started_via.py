"""How a run was started, for the tool permission rules

- completions.started_via: 'api_key' on the head (user) completion of a run
  started by an API-key request; such runs are unattended. NULL for every
  other run.

Nullable, no backfill.

Revision ID: permmode02
Revises: permmode01
Create Date: 2026-11-17
"""
import sqlalchemy as sa

from alembic import op

revision = "permmode02"
down_revision = "permmode01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("completions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("started_via", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("completions", schema=None) as batch_op:
        batch_op.drop_column("started_via")
