"""add ldap_dn to users and index on groups.external_provider

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-24 00:00:00.000000

Adds LDAP integration support:
- ldap_dn column on users for caching LDAP distinguished name
- Index on groups.external_provider for efficient filtered queries
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6g7'
down_revision: str = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('ldap_dn', sa.String(512), nullable=True))
    op.create_index('ix_users_ldap_dn', 'users', ['ldap_dn'])
    op.create_index('ix_groups_external_provider', 'groups', ['external_provider'])


def downgrade() -> None:
    op.drop_index('ix_groups_external_provider', table_name='groups')
    op.drop_index('ix_users_ldap_dn', table_name='users')
    op.drop_column('users', 'ldap_dn')
