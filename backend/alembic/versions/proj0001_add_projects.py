"""add projects (shared folders for reports)

Revision ID: proj0001
Revises: entraprof01
Create Date: 2026-07-25 00:00:00.000000

Adds the projects table (private-by-default shared folders), the
project default-agent / default-file association tables, and the
nullable reports.project_id membership FK.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'proj0001'
down_revision: Union[str, None] = 'rfv1o2n3v4w5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'projects',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False, unique=True, index=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('color', sa.String(), nullable=True),
        sa.Column('access', sa.String(), nullable=False, server_default='private'),
        sa.Column('settings', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_projects_name', 'projects', ['name'])
    op.create_index('ix_projects_user_id', 'projects', ['user_id'])
    op.create_index('ix_projects_organization_id', 'projects', ['organization_id'])

    op.create_table(
        'project_data_source_association',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id')),
        sa.Column('data_source_id', sa.String(36), sa.ForeignKey('data_sources.id')),
    )
    op.create_table(
        'project_file_association',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id')),
        sa.Column('file_id', sa.String(36), sa.ForeignKey('files.id')),
    )

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key('fk_reports_project_id', 'projects', ['project_id'], ['id'])
        batch_op.create_index('ix_reports_project_id', ['project_id'])


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_constraint('fk_reports_project_id', type_='foreignkey')
        batch_op.drop_index('ix_reports_project_id')
        batch_op.drop_column('project_id')

    op.drop_table('project_file_association')
    op.drop_table('project_data_source_association')
    op.drop_index('ix_projects_organization_id', table_name='projects')
    op.drop_index('ix_projects_user_id', table_name='projects')
    op.drop_index('ix_projects_name', table_name='projects')
    op.drop_table('projects')
