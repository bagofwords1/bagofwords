"""Add detected_project_types to metadata_indexing_jobs

Revision ID: 4e6982618f6e
Revises: 6b55dcdbf53f
Create Date: 2025-04-07 21:17:23.537003

"""
from typing import Sequence, Union
import asyncio
import tempfile
import shutil
import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '4e6982618f6e'
down_revision: Union[str, None] = '6b55dcdbf53f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Schema changes only
    op.create_table('metadata_resources',
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('resource_type', sa.String(), nullable=False),
    sa.Column('path', sa.String(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('raw_data', sa.JSON(), nullable=True),
    sa.Column('sql_content', sa.Text(), nullable=True),
    sa.Column('source_name', sa.String(), nullable=True),
    sa.Column('database', sa.String(), nullable=True),
    sa.Column('schema', sa.String(), nullable=True),
    sa.Column('columns', sa.JSON(), nullable=True),
    sa.Column('depends_on', sa.JSON(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('data_source_id', sa.String(length=36), nullable=False),
    sa.Column('metadata_indexing_job_id', sa.String(length=36), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ),
    sa.ForeignKeyConstraint(['metadata_indexing_job_id'], ['metadata_indexing_jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('metadata_resources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_metadata_resources_id'), ['id'], unique=True)

    with op.batch_alter_table('metadata_indexing_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('detected_project_types', sa.JSON(), nullable=True))

    # Drop the old dbt_resources table
    with op.batch_alter_table('dbt_resources', schema=None) as batch_op:
        batch_op.drop_index('ix_dbt_resources_id')

    op.drop_table('dbt_resources')

    # ### end Alembic commands ###


def downgrade() -> None:
    with op.batch_alter_table('metadata_indexing_jobs', schema=None) as batch_op:
        batch_op.drop_column('detected_project_types')

    op.create_table('dbt_resources',
    sa.Column('name', sa.VARCHAR(), nullable=False),
    sa.Column('resource_type', sa.VARCHAR(), nullable=False),
    sa.Column('path', sa.VARCHAR(), nullable=True),
    sa.Column('description', sa.TEXT(), nullable=True),
    sa.Column('raw_data', sqlite.JSON(), nullable=True),
    sa.Column('sql_content', sa.TEXT(), nullable=True),
    sa.Column('source_name', sa.VARCHAR(), nullable=True),
    sa.Column('database', sa.VARCHAR(), nullable=True),
    sa.Column('schema', sa.VARCHAR(), nullable=True),
    sa.Column('columns', sqlite.JSON(), nullable=True),
    sa.Column('depends_on', sqlite.JSON(), nullable=True),
    sa.Column('is_active', sa.BOOLEAN(), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('data_source_id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('metadata_indexing_job_id', sa.VARCHAR(length=36), nullable=True),
    sa.Column('id', sa.VARCHAR(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ),
    sa.ForeignKeyConstraint(['metadata_indexing_job_id'], ['metadata_indexing_jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('dbt_resources', schema=None) as batch_op:
        batch_op.create_index('ix_dbt_resources_id', ['id'], unique=1)

    with op.batch_alter_table('metadata_resources', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_metadata_resources_id'))

    op.drop_table('metadata_resources')
    # ### end Alembic commands ###
