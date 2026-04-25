"""add events_json to connection_indexings

Revision ID: d6e7f8a9b0c1
Revises: f1a2b3c4d5e7
Create Date: 2026-04-25 09:00:00.000000

Per-run event log for indexing — phase transitions, item milestones, errors,
completion. Capped at ~200 entries by the runner; this column is just storage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, None] = 'f1a2b3c4d5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('connection_indexings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('events_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('connection_indexings', schema=None) as batch_op:
        batch_op.drop_column('events_json')
