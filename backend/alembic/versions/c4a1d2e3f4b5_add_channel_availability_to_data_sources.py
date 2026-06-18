"""add channel_availability to data sources

Revision ID: c4a1d2e3f4b5
Revises: e7f0a1b2c3d4, g2h3i4j5k6l7
Create Date: 2026-06-15 09:00:00.000000

Adds the ``channel_availability`` JSON column to ``data_sources`` so an agent
can be configured to be available (queryable) only in a subset of the
organization's connected channels (Slack, Teams, WhatsApp, email, MCP).

Stored as a JSON map of ``{channel_type: bool}``. ``NULL`` (the backfill value
for existing rows) means "available in every connected channel", so this
migration is behaviour-preserving.

This revision also merges the two open migration heads
(``e7f0a1b2c3d4`` and ``g2h3i4j5k6l7``) into a single head.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4a1d2e3f4b5'
down_revision: Union[str, Sequence[str], None] = ('e7f0a1b2c3d4', 'g2h3i4j5k6l7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.add_column(
            sa.Column('channel_availability', sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.drop_column('channel_availability')
