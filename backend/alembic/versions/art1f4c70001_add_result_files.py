"""add result_files (result store handle rows)

Revision ID: art1f4c70001
Revises: m1e2r3g4e5f6
Create Date: 2026-07-09 00:00:00.000000

Handle rows for the Result Store: large tool results are
persisted as encrypted DuckDB files on shared storage; this table is the
control plane (key, scope, retention, rerun lineage). Payload files live
outside the DB; a row with status='published' guarantees an attachable file.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'art1f4c70001'
down_revision: Union[str, None] = 'm1e2r3g4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'result_files',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('report_id', sa.String(36), sa.ForeignKey('reports.id'), nullable=True),
        sa.Column('step_id', sa.String(36), sa.ForeignKey('steps.id'), nullable=True),
        sa.Column('query_id', sa.String(36), sa.ForeignKey('queries.id'), nullable=True),
        sa.Column('tool_execution_id', sa.String(36), nullable=True),
        sa.Column('producer', sa.String(), nullable=False, server_default='create_data'),
        sa.Column('content_type', sa.String(), nullable=False, server_default='table'),
        sa.Column('schema_json', sa.JSON(), nullable=True),
        sa.Column('ts_column', sa.String(), nullable=True),
        sa.Column('row_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('byte_size', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('storage_ref', sa.String(), nullable=False),
        sa.Column('format', sa.String(), nullable=False, server_default='duckdb'),
        sa.Column('content_sha256', sa.String(), nullable=True),
        sa.Column('wrapped_key', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='published'),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('legal_hold', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('cited', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('superseded_by', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_result_files_organization_id', 'result_files', ['organization_id'])
    op.create_index('ix_result_files_report_id', 'result_files', ['report_id'])
    op.create_index('ix_result_files_step_id', 'result_files', ['step_id'])
    op.create_index('ix_result_files_query_id', 'result_files', ['query_id'])


def downgrade() -> None:
    op.drop_index('ix_result_files_query_id', table_name='result_files')
    op.drop_index('ix_result_files_step_id', table_name='result_files')
    op.drop_index('ix_result_files_report_id', table_name='result_files')
    op.drop_index('ix_result_files_organization_id', table_name='result_files')
    op.drop_table('result_files')
