"""add app_records table (artifact app persistence)

Revision ID: apprec01
Revises: artchatmodel01
Create Date: 2026-09-27 12:00:00.000000

One row per record of an artifact app's data: a fixed envelope the server
filters and authorizes on, plus the app-defined `data` JSON (EncryptedJSON in
the model, a plain JSON column here). Generic types only so the schema is the
same on SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'apprec01'
down_revision: Union[str, None] = 'artchatmodel01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_records',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('artifact_id', sa.String(36), sa.ForeignKey('artifacts.id'), nullable=False),
        sa.Column('collection', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_app_records_id', 'app_records', ['id'], unique=True)
    op.create_index('ix_app_records_organization_id', 'app_records', ['organization_id'])
    op.create_index('ix_app_records_report_id', 'app_records', ['report_id'])
    op.create_index('ix_app_records_artifact_collection', 'app_records', ['artifact_id', 'collection'])
    op.create_index(
        'ix_app_records_artifact_collection_user', 'app_records', ['artifact_id', 'collection', 'user_id']
    )


def downgrade() -> None:
    op.drop_index('ix_app_records_artifact_collection_user', table_name='app_records')
    op.drop_index('ix_app_records_artifact_collection', table_name='app_records')
    op.drop_index('ix_app_records_report_id', table_name='app_records')
    op.drop_index('ix_app_records_organization_id', table_name='app_records')
    op.drop_index('ix_app_records_id', table_name='app_records')
    op.drop_table('app_records')
