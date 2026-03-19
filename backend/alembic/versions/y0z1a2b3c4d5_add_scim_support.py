"""add scim_tokens table and scim_external_id to users

Revision ID: y0z1a2b3c4d5
Revises: x9y0z1a2b3c4
Create Date: 2026-03-19 00:00:00.000000

Adds SCIM 2.0 provisioning support:
- scim_tokens table for IdP bearer token authentication
- scim_external_id column on users for IdP external identifier
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'y0z1a2b3c4d5'
down_revision: str = 'x9y0z1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scim_tokens',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True, index=True),
        sa.Column('token_prefix', sa.String(16), nullable=False),
        sa.Column('created_by_user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('last_used_at', sa.DateTime, nullable=True),
        sa.Column('expires_at', sa.DateTime, nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=True),
        sa.Column('updated_at', sa.DateTime, nullable=True),
        sa.Column('deleted_at', sa.DateTime, nullable=True),
    )

    op.add_column('users', sa.Column('scim_external_id', sa.String(255), nullable=True))
    op.create_index('ix_users_scim_external_id', 'users', ['scim_external_id'])


def downgrade() -> None:
    op.drop_index('ix_users_scim_external_id', table_name='users')
    op.drop_column('users', 'scim_external_id')
    op.drop_table('scim_tokens')
