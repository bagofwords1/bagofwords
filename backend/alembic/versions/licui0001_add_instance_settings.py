"""add instance_settings table (license key set from the UI)

Instance-wide settings that are not properties of any single organization.
First user: the enterprise license key, so admins can activate/renew a license
from Settings → License instead of redeploying with a new BOW_LICENSE_KEY.

Revision ID: licui0001
Revises: instrdir01
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa


revision = 'licui0001'
down_revision = 'instrdir01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'instance_settings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_instance_settings_id', 'instance_settings', ['id'], unique=False)
    op.create_index('ix_instance_settings_key', 'instance_settings', ['key'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_instance_settings_key', table_name='instance_settings')
    op.drop_index('ix_instance_settings_id', table_name='instance_settings')
    op.drop_table('instance_settings')
