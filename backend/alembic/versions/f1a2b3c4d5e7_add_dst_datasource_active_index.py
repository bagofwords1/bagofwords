"""add composite index on datasource_tables(datasource_id, is_active)

Revision ID: f1a2b3c4d5e7
Revises: b4c5d6e7f8a9
Create Date: 2026-04-24 00:00:00.000000

Customers with thousands of datasource_tables per data source (mostly inactive)
were seeing full seqscans on every schema_context_builder query — which also
pulls large JSON columns (columns/pks/fks/metadata_json) during filtering.
Composite index lets the filter `datasource_id = ? AND is_active = True` be
index-only for row selection.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f1a2b3c4d5e7'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ix_dst_ds_active',
        'datasource_tables',
        ['datasource_id', 'is_active'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_dst_ds_active', table_name='datasource_tables')
