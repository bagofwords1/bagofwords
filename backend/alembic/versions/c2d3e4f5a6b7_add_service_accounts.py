"""add service accounts

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-26 00:00:00.000000

Adds the `service_accounts` table (org-managed non-human API principal, backed
by a hidden `users` row), a `users.is_service_account` flag, and a nullable
`api_keys.service_account_id` column so a key can be scoped to a service
account. No data migration: existing users get is_service_account=false and
existing keys get service_account_id=NULL (personal keys).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'service_accounts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('disabled_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_service_accounts_user_id'),
    )
    op.create_index('ix_service_accounts_organization_id', 'service_accounts', ['organization_id'])
    op.create_index('ix_service_accounts_user_id', 'service_accounts', ['user_id'])

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_service_account', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index('ix_users_is_service_account', ['is_service_account'])

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('service_account_id', sa.String(length=36), nullable=True))
        batch_op.create_index('ix_api_keys_service_account_id', ['service_account_id'])
        batch_op.create_foreign_key(
            'fk_api_keys_service_account_id',
            'service_accounts', ['service_account_id'], ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_constraint('fk_api_keys_service_account_id', type_='foreignkey')
        batch_op.drop_index('ix_api_keys_service_account_id')
        batch_op.drop_column('service_account_id')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_is_service_account')
        batch_op.drop_column('is_service_account')

    op.drop_index('ix_service_accounts_user_id', table_name='service_accounts')
    op.drop_index('ix_service_accounts_organization_id', table_name='service_accounts')
    op.drop_table('service_accounts')
