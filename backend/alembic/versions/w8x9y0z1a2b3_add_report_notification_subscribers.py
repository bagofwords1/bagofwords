"""add notification_subscribers column to reports

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
Create Date: 2026-03-11 00:00:00.000000

Stores per-report notification subscriber list for scheduled reruns.
Each entry is either {"type": "user", "id": "..."} or {"type": "email", "address": "..."}.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'w8x9y0z1a2b3'
down_revision: Union[str, None] = 'v7w8x9y0z1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notification_subscribers', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_column('notification_subscribers')
