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


TABLE = "completions"
COLUMN = "started_via"


def _has_column() -> bool:
    return COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped, so a later
    # `upgrade head` runs it again.
    if _has_column():
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.String(length=16), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
