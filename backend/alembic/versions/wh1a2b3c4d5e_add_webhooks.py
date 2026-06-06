"""add webhooks table and completions.webhook_id

Revision ID: wh1a2b3c4d5e
Revises: overlay_fk_ondelete
Create Date: 2026-06-06 20:00:00.000000

Adds inbound webhook support:
- `webhooks` table (per-report, HMAC/token verified, optional AI classifier)
- `completions.webhook_id` FK for provenance + hiding the internal trigger
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'wh1a2b3c4d5e'
down_revision: Union[str, None] = 'overlay_fk_ondelete'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'webhooks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('report_id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('token', sa.String(), nullable=False),
        sa.Column('secret_encrypted', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('auth_mode', sa.String(), nullable=False),
        sa.Column('auth_header_name', sa.String(), nullable=True),
        sa.Column('classify_enabled', sa.Boolean(), nullable=False),
        sa.Column('classifier_prompt', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('last_delivery_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_webhooks_id'), 'webhooks', ['id'], unique=False)
    op.create_index(op.f('ix_webhooks_token'), 'webhooks', ['token'], unique=True)
    op.create_index(op.f('ix_webhooks_report_id'), 'webhooks', ['report_id'], unique=False)
    op.create_index(op.f('ix_webhooks_organization_id'), 'webhooks', ['organization_id'], unique=False)

    with op.batch_alter_table('completions') as batch_op:
        batch_op.add_column(sa.Column('webhook_id', sa.String(length=36), nullable=True))
        batch_op.create_index(op.f('ix_completions_webhook_id'), ['webhook_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('completions') as batch_op:
        batch_op.drop_index(op.f('ix_completions_webhook_id'))
        batch_op.drop_column('webhook_id')

    op.drop_index(op.f('ix_webhooks_organization_id'), table_name='webhooks')
    op.drop_index(op.f('ix_webhooks_report_id'), table_name='webhooks')
    op.drop_index(op.f('ix_webhooks_token'), table_name='webhooks')
    op.drop_index(op.f('ix_webhooks_id'), table_name='webhooks')
    op.drop_table('webhooks')
