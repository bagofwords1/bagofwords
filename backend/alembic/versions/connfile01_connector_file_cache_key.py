"""add source_connection_id / source_ref to files

Revision ID: connfile01
Revises: instrdir01
Create Date: 2026-08-03 10:00:00.000000

Connector files are a CACHE of a remote file, not an upload, but nothing on the
row said WHICH remote file — so every read minted a fresh File row and a fresh
copy on disk, none of them linked to the report. These two columns give the
cache a key: refresh the existing row for a (report, connection, ref) instead
of accumulating duplicates.

NULL for uploads, which have no remote counterpart.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'connfile01'
down_revision: Union[str, None] = 'instrdir01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('files', sa.Column('source_connection_id', sa.String(36), nullable=True))
    op.add_column('files', sa.Column('source_ref', sa.String(), nullable=True))
    op.create_index('ix_files_source_connection_id', 'files', ['source_connection_id'])
    op.create_index('ix_files_source_ref', 'files', ['source_ref'])


def downgrade() -> None:
    op.drop_index('ix_files_source_ref', table_name='files')
    op.drop_index('ix_files_source_connection_id', table_name='files')
    op.drop_column('files', 'source_ref')
    op.drop_column('files', 'source_connection_id')
