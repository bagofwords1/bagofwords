"""add slack thread columns to completions

Revision ID: m8n9o0p1q2r3
Revises: l7m8n9o0p1q2
Create Date: 2025-01-19 12:00:00.000000

Adds columns to completions table for Slack thread-based responses:
- external_thread_ts: Thread parent timestamp for responding in threads
- external_message_ts: User's message timestamp for adding/removing reactions
- external_channel_id: Channel ID needed for reaction API calls
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'm8n9o0p1q2r3'
down_revision: Union[str, None] = 'l7m8n9o0p1q2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('external_thread_ts', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('external_message_ts', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('external_channel_id', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('external_channel_type', sa.String(), nullable=True))
        batch_op.create_index('ix_completions_external_thread_ts', ['external_thread_ts'])


def downgrade() -> None:
    with op.batch_alter_table('completions', schema=None) as batch_op:
        batch_op.drop_index('ix_completions_external_thread_ts')
        batch_op.drop_column('external_channel_type')
        batch_op.drop_column('external_channel_id')
        batch_op.drop_column('external_message_ts')
        batch_op.drop_column('external_thread_ts')
