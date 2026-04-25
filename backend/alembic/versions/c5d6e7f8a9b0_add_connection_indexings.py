"""add connection_indexings table

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-04-24 20:00:00.000000

Background-job tracking for schema discovery on a Connection. One row per
refresh attempt. The service layer enforces at most one non-terminal row
per connection.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'connection_indexings',
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column('phase', sa.String(length=64), nullable=True),
        sa.Column('current_item', sa.String(), nullable=True),
        sa.Column('progress_done', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('progress_total', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('stats_json', sa.JSON(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('connection_indexings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_connection_indexings_id'), ['id'], unique=True)
        batch_op.create_index(batch_op.f('ix_connection_indexings_connection_id'), ['connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_connection_indexings_status'), ['status'], unique=False)
        batch_op.create_index(
            'ix_connection_indexings_conn_created',
            ['connection_id', 'created_at'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('connection_indexings', schema=None) as batch_op:
        batch_op.drop_index('ix_connection_indexings_conn_created')
        batch_op.drop_index(batch_op.f('ix_connection_indexings_status'))
        batch_op.drop_index(batch_op.f('ix_connection_indexings_connection_id'))
        batch_op.drop_index(batch_op.f('ix_connection_indexings_id'))
    op.drop_table('connection_indexings')
