"""add BOW custom queries (kind='bow') to connection_tables

Revision ID: fastq001
Revises: bc0001chg
Create Date: 2026-07-28 00:00:00.000000

A custom query is admin-authored SQL on a connection, materialized to an
encrypted local DuckDB artifact on a schedule and served to agents from there
instead of the source. It lives in connection_tables (rather than a parallel
model) so agent activation, per-user overlays, table_stats and schema-context
rendering keep working unchanged.

`kind` is 'table' for introspected rows and 'bow' for custom queries —
deliberately not 'view', which would imply non-materialized.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fastq001'
down_revision: Union[str, None] = 'bc0001chg'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'connection_tables',
        sa.Column('kind', sa.String(), nullable=False, server_default='table'),
    )
    op.add_column('connection_tables', sa.Column('definition_sql', sa.Text(), nullable=True))
    op.add_column('connection_tables', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('connection_tables', sa.Column('refresh_schedule_mode', sa.String(), nullable=True))
    op.add_column('connection_tables', sa.Column('refresh_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('connection_tables', sa.Column('refresh_at_time', sa.String(), nullable=True))
    op.add_column('connection_tables', sa.Column('last_refreshed_at', sa.DateTime(), nullable=True))
    op.add_column('connection_tables', sa.Column('last_refresh_status', sa.String(), nullable=True))
    op.add_column('connection_tables', sa.Column('last_refresh_error', sa.Text(), nullable=True))
    op.add_column('connection_tables', sa.Column('last_refresh_ms', sa.Integer(), nullable=True))
    op.add_column('connection_tables', sa.Column('artifact_path', sa.String(), nullable=True))
    op.add_column('connection_tables', sa.Column('artifact_key_enc', sa.Text(), nullable=True))
    op.add_column('connection_tables', sa.Column('artifact_bytes', sa.BigInteger(), nullable=True))

    with op.batch_alter_table('connection_tables') as batch_op:
        batch_op.create_index('ix_connection_tables_kind', ['connection_id', 'kind'])


def downgrade() -> None:
    with op.batch_alter_table('connection_tables') as batch_op:
        batch_op.drop_index('ix_connection_tables_kind')

    for col in (
        'artifact_bytes', 'artifact_key_enc', 'artifact_path',
        'last_refresh_ms', 'last_refresh_error', 'last_refresh_status',
        'last_refreshed_at', 'refresh_at_time', 'refresh_interval_minutes',
        'refresh_schedule_mode', 'description', 'definition_sql', 'kind',
    ):
        op.drop_column('connection_tables', col)
