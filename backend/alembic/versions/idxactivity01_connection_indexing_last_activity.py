"""add last_activity_at to connection_indexings

Revision ID: idxactivity01
Revises: codeperm01
Create Date: 2026-09-16

When the source last reported progress for a run. Distinct from `updated_at`,
which the runner's liveness heartbeat touches every 30s whether or not the run
is getting anywhere: a run can be alive (heartbeating) and stuck (no activity)
at the same time, and telling those apart is the whole point of this column —
it drives the UI's "no activity for N minutes" warning and is what the
inactivity timeout measures.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'idxactivity01'
down_revision: Union[str, None] = 'codeperm01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('connection_indexings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_activity_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('connection_indexings', schema=None) as batch_op:
        batch_op.drop_column('last_activity_at')
