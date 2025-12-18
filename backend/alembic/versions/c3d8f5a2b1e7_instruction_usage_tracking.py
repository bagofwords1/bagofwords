"""instruction usage tracking

Revision ID: c3d8f5a2b1e7
Revises: b8f3a2c91d4e
Create Date: 2025-12-18 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d8f5a2b1e7'
down_revision: Union[str, None] = 'b8f3a2c91d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create instruction_usage_events table
    op.create_table('instruction_usage_events',
        sa.Column('org_id', sa.String(length=36), nullable=False),
        sa.Column('report_id', sa.String(length=36), nullable=True),
        sa.Column('instruction_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=True),
        sa.Column('load_mode', sa.String(length=20), nullable=False),
        sa.Column('load_reason', sa.String(length=50), nullable=True),
        sa.Column('search_score', sa.Float(), nullable=True),
        sa.Column('search_query_keywords', sa.JSON(), nullable=True),
        sa.Column('source_type', sa.String(length=32), nullable=True),
        sa.Column('category', sa.String(length=50), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('user_role', sa.String(length=64), nullable=True),
        sa.Column('role_weight', sa.Float(), nullable=True),
        sa.Column('used_at', sa.DateTime(), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['instruction_id'], ['instructions.id'], ),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('instruction_usage_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_instruction_usage_events_id'), ['id'], unique=True)
        batch_op.create_index('ix_inst_usage_org_inst_time', ['org_id', 'instruction_id', 'used_at'], unique=False)
        batch_op.create_index('ix_inst_usage_report_time', ['report_id', 'used_at'], unique=False)
        batch_op.create_index('ix_inst_usage_load_mode', ['org_id', 'load_mode', 'used_at'], unique=False)

    # Create instruction_feedback_events table
    op.create_table('instruction_feedback_events',
        sa.Column('org_id', sa.String(length=36), nullable=False),
        sa.Column('report_id', sa.String(length=36), nullable=True),
        sa.Column('instruction_id', sa.String(length=36), nullable=False),
        sa.Column('completion_feedback_id', sa.String(length=36), nullable=False),
        sa.Column('feedback_type', sa.String(length=16), nullable=False),
        sa.Column('created_at_event', sa.DateTime(), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['completion_feedback_id'], ['completion_feedbacks.id'], ),
        sa.ForeignKeyConstraint(['instruction_id'], ['instructions.id'], ),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('instruction_feedback_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_instruction_feedback_events_id'), ['id'], unique=True)
        batch_op.create_index('ix_inst_fb_org_inst_time', ['org_id', 'instruction_id', 'created_at_event'], unique=False)
        batch_op.create_index('ix_inst_fb_completion', ['completion_feedback_id'], unique=False)

    # Create instruction_stats table
    op.create_table('instruction_stats',
        sa.Column('org_id', sa.String(length=36), nullable=False),
        sa.Column('report_id', sa.String(length=36), nullable=True),
        sa.Column('instruction_id', sa.String(length=36), nullable=False),
        sa.Column('usage_count', sa.BigInteger(), nullable=False),
        sa.Column('always_count', sa.BigInteger(), nullable=False),
        sa.Column('intelligent_count', sa.BigInteger(), nullable=False),
        sa.Column('mentioned_count', sa.BigInteger(), nullable=False),
        sa.Column('weighted_usage_count', sa.Float(), nullable=False),
        sa.Column('pos_feedback_count', sa.BigInteger(), nullable=False),
        sa.Column('neg_feedback_count', sa.BigInteger(), nullable=False),
        sa.Column('weighted_pos_feedback', sa.Float(), nullable=False),
        sa.Column('weighted_neg_feedback', sa.Float(), nullable=False),
        sa.Column('unique_users', sa.BigInteger(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('last_feedback_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at_stats', sa.DateTime(), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['instruction_id'], ['instructions.id'], ),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('instruction_stats', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_instruction_stats_id'), ['id'], unique=True)
        batch_op.create_index('ix_inststats_org_report_inst', ['org_id', 'report_id', 'instruction_id'], unique=True)
        batch_op.create_index('ix_inststats_instruction', ['instruction_id'], unique=False)
        batch_op.create_index('ix_inststats_usage', ['org_id', 'usage_count'], unique=False)


def downgrade() -> None:
    # Drop instruction_stats
    with op.batch_alter_table('instruction_stats', schema=None) as batch_op:
        batch_op.drop_index('ix_inststats_usage')
        batch_op.drop_index('ix_inststats_instruction')
        batch_op.drop_index('ix_inststats_org_report_inst')
        batch_op.drop_index(batch_op.f('ix_instruction_stats_id'))
    op.drop_table('instruction_stats')

    # Drop instruction_feedback_events
    with op.batch_alter_table('instruction_feedback_events', schema=None) as batch_op:
        batch_op.drop_index('ix_inst_fb_completion')
        batch_op.drop_index('ix_inst_fb_org_inst_time')
        batch_op.drop_index(batch_op.f('ix_instruction_feedback_events_id'))
    op.drop_table('instruction_feedback_events')

    # Drop instruction_usage_events
    with op.batch_alter_table('instruction_usage_events', schema=None) as batch_op:
        batch_op.drop_index('ix_inst_usage_load_mode')
        batch_op.drop_index('ix_inst_usage_report_time')
        batch_op.drop_index('ix_inst_usage_org_inst_time')
        batch_op.drop_index(batch_op.f('ix_instruction_usage_events_id'))
    op.drop_table('instruction_usage_events')
