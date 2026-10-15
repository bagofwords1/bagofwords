"""Durable parent/child messages.

Revision ID: submsg01
Revises: mrgheads10
"""
import sqlalchemy as sa

from alembic import op

revision = 'submsg01'
down_revision = 'mrgheads10'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('sub_agent_messages',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('created_at', sa.DateTime()), sa.Column('updated_at', sa.DateTime()),
        sa.Column('deleted_at', sa.DateTime()),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('wave_id', sa.String(36), sa.ForeignKey('sub_agent_waves.id'), nullable=False),
        sa.Column('sub_agent_id', sa.String(36), sa.ForeignKey('sub_agent_runs.id'), nullable=False),
        sa.Column('sender_sub_agent_id', sa.String(36), sa.ForeignKey('sub_agent_runs.id')),
        sa.Column('recipient', sa.String(36), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False), sa.Column('body', sa.Text(), nullable=False),
        sa.Column('reply_to', sa.String(36), sa.ForeignKey('sub_agent_messages.id')),
        sa.Column('idempotency_key', sa.String(160), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('delivered_at', sa.DateTime()), sa.Column('reason', sa.String(80)),
        sa.UniqueConstraint('report_id', 'idempotency_key', name='uq_sub_agent_message_send'),
    )
    op.create_index('ix_sub_agent_messages_inbox', 'sub_agent_messages', ['report_id', 'recipient', 'status'])
    op.create_index('ix_sub_agent_messages_child', 'sub_agent_messages', ['sub_agent_id', 'created_at'])


def downgrade():
    op.drop_table('sub_agent_messages')
