"""organizations and memberships

Revision ID: 8f06b9c7f785
Revises: 6883ef8e3d7c
Create Date: 2024-08-18 21:26:59.162616

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column


# revision identifiers, used by Alembic.
revision: str = '8f06b9c7f785'
down_revision: Union[str, None] = '6883ef8e3d7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # Create memberships table
        op.create_table('memberships',
            sa.Column('user_id', get_uuid_column(), nullable=True),
            sa.Column('organization_id', get_uuid_column(), nullable=False),
            sa.Column('role', sa.String(), nullable=False),
            sa.Column('id', get_uuid_column(), nullable=False),
            sa.Column('email', sa.String(), nullable=True),
            sa.Column('invite_token', sa.String(36), nullable=True, unique=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('deleted_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_memberships_organization_id'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_memberships_user_id'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_memberships_id', 'memberships', ['id'], unique=True)

        # Update files table
        with op.batch_alter_table('files') as batch_op:
            batch_op.add_column(sa.Column('organization_id', get_uuid_column(), nullable=False))
            batch_op.create_foreign_key('fk_files_organization_id', 'organizations', ['organization_id'], ['id'])

        # Update organizations table
        with op.batch_alter_table('organizations') as batch_op:
            batch_op.create_index('ix_organizations_id', ['id'], unique=True)

        # Update reports table
        with op.batch_alter_table('reports') as batch_op:
            batch_op.add_column(sa.Column('organization_id', get_uuid_column(), nullable=False))
            batch_op.create_index('ix_reports_id', ['id'], unique=True)
            batch_op.create_index('ix_reports_organization_id', ['organization_id'], unique=False)
            batch_op.create_foreign_key('fk_reports_organization_id', 'organizations', ['organization_id'], ['id'])

    else:
        # PostgreSQL path
        op.create_table('memberships',
            sa.Column('user_id', get_uuid_column(), nullable=True),
            sa.Column('organization_id', get_uuid_column(), nullable=False),
            sa.Column('role', sa.String(), nullable=False),
            sa.Column('id', get_uuid_column(), nullable=False),
            sa.Column('email', sa.String(), nullable=True),
            sa.Column('invite_token', sa.String(36), nullable=True, unique=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.Column('deleted_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_memberships_organization_id'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_memberships_user_id'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_memberships_id', 'memberships', ['id'], unique=True)
        op.add_column('files', sa.Column('organization_id', get_uuid_column(), nullable=False))
        op.create_foreign_key('fk_files_organization_id', 'files', 'organizations', ['organization_id'], ['id'])
        op.create_index('ix_organizations_id', 'organizations', ['id'], unique=True)
        op.add_column('reports', sa.Column('organization_id', get_uuid_column(), nullable=False))
        op.create_index('ix_reports_id', 'reports', ['id'], unique=True)
        op.create_index('ix_reports_organization_id', 'reports', ['organization_id'], unique=False)
        op.create_foreign_key('fk_reports_organization_id', 'reports', 'organizations', ['organization_id'], ['id'])


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # SQLite path
        with op.batch_alter_table('reports') as batch_op:
            batch_op.drop_constraint('fk_reports_organization_id', type_='foreignkey')
            batch_op.drop_index('ix_reports_organization_id')
            batch_op.drop_index('ix_reports_id')
            batch_op.drop_column('organization_id')

        with op.batch_alter_table('organizations') as batch_op:
            batch_op.drop_index('ix_organizations_id')

        with op.batch_alter_table('files') as batch_op:
            batch_op.drop_constraint('fk_files_organization_id', type_='foreignkey')
            batch_op.drop_column('organization_id')

        op.drop_index('ix_memberships_id', table_name='memberships')
        op.drop_table('memberships')

    else:
        # PostgreSQL path
        op.drop_index('ix_reports_organization_id', table_name='reports')
        op.drop_index('ix_reports_id', table_name='reports')
        op.drop_column('reports', 'organization_id')
        op.drop_index('ix_organizations_id', table_name='organizations')
        op.drop_constraint('fk_files_organization_id', 'files', type_='foreignkey')
        op.drop_column('files', 'organization_id')
        op.drop_index('ix_memberships_id', table_name='memberships')
        op.drop_table('memberships')
