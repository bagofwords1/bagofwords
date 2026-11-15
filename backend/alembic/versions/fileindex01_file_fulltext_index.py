"""full-text index for file connections: file_index_files / file_index_sections

Revision ID: fileindex01
Revises: shwinfo01
Create Date: 2026-11-12 00:00:00.000000

Additive only. Two new tables back the ``fulltext`` index tier of file
connections (see app/models/file_index.py). The searchable terms are stored in
a dialect-specific structure:

- Postgres: a ``tsv tsvector`` column on file_index_sections with a GIN index.
- SQLite:   an FTS5 table ``file_index_fts`` keyed by the section row id. It is
            contentless — it holds the index, not the text.

Nothing is backfilled: a connection is indexed when an admin sets its indexing
mode to Full text and the next index run completes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fileindex01'
down_revision: Union[str, None] = 'shwinfo01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    big = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    existing = _tables()

    if 'file_index_files' not in existing:
        op.create_table(
            'file_index_files',
            sa.Column('id', big, primary_key=True, autoincrement=True),
            sa.Column('connection_id', sa.String(length=36),
                      sa.ForeignKey('connections.id', ondelete='CASCADE'), nullable=False),
            sa.Column('file_key', sa.String(length=40), nullable=False),
            sa.Column('file_id', sa.Text(), nullable=False),
            sa.Column('path', sa.Text(), nullable=True),
            sa.Column('version', sa.String(length=255), nullable=True),
            sa.Column('status', sa.String(length=16), nullable=False, server_default='indexed'),
            sa.Column('section_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('char_count', big, nullable=False, server_default='0'),
            sa.Column('index_version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('meta', sa.Text(), nullable=True),
            sa.Column('indexed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('connection_id', 'file_key', name='uq_file_index_files_conn_key'),
        )
        op.create_index('ix_file_index_files_connection_id', 'file_index_files', ['connection_id'])

    if 'file_index_sections' not in existing:
        op.create_table(
            'file_index_sections',
            sa.Column('id', big, primary_key=True, autoincrement=True),
            sa.Column('connection_id', sa.String(length=36), nullable=False),
            sa.Column('file_ref', big,
                      sa.ForeignKey('file_index_files.id', ondelete='CASCADE'), nullable=False),
            sa.Column('section_no', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('kind', sa.String(length=16), nullable=False),
            sa.Column('loc', sa.Text(), nullable=True),
            sa.Column('token_count', sa.Integer(), nullable=False, server_default='0'),
        )
        op.create_index('ix_file_index_sections_connection_id', 'file_index_sections', ['connection_id'])
        op.create_index('ix_file_index_sections_file_ref', 'file_index_sections', ['file_ref'])

    if bind.dialect.name == 'postgresql':
        op.execute("ALTER TABLE file_index_sections ADD COLUMN IF NOT EXISTS tsv tsvector")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_file_index_sections_tsv "
            "ON file_index_sections USING GIN (tsv)"
        )
    elif bind.dialect.name == 'sqlite':
        # terms: every word in order (phrases match here); alts: alternative
        # forms of those words (Hebrew without its particle prefix).
        op.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS file_index_fts USING fts5("
            "terms, alts, content='', contentless_delete=1, "
            "tokenize=\"unicode61 remove_diacritics 0\")"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        op.execute("DROP TABLE IF EXISTS file_index_fts")
    op.drop_table('file_index_sections')
    op.drop_table('file_index_files')
