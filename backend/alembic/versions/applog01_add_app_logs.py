"""add app_logs table (Settings → System logs)

The application's own stdout/stderr output, copied into the database while an
admin has System logs switched on. Instance-wide, not per organization. The
on/off switch lives in instance_settings under the key "app_logs".

Revision ID: applog01
Revises: fileindex01
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa


revision = 'applog01'
down_revision = 'fileindex01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'app_logs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('instance_id', sa.String(length=255), nullable=False),
        sa.Column('level', sa.String(length=16), nullable=True),
        sa.Column('logger', sa.String(length=255), nullable=True),
        sa.Column('stream', sa.String(length=8), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_app_logs_timestamp', 'app_logs', ['timestamp'], unique=False)
    # Also serves instance_id-only lookups (leading column), so no separate
    # instance_id index: one less index write per row on the busiest table.
    op.create_index('ix_app_logs_instance_timestamp', 'app_logs', ['instance_id', 'timestamp'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_app_logs_instance_timestamp', table_name='app_logs')
    op.drop_index('ix_app_logs_timestamp', table_name='app_logs')
    op.drop_table('app_logs')
