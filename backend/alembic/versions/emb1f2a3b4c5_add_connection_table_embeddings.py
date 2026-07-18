"""add embedding columns to connection_tables

Semantic file search: per-file vector + model tag + source content hash on
connection_tables. Nullable/backfilled lazily; no data migration.

Revision ID: emb1f2a3b4c5
Revises: umbr0001
Create Date: 2026-07-17

"""
from typing import Union, Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'emb1f2a3b4c5'
down_revision: Union[str, None] = 'umbr0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("connection_tables", "embedding"):
        op.add_column("connection_tables", sa.Column("embedding", sa.JSON(), nullable=True))
    if not _has_column("connection_tables", "embedding_model"):
        op.add_column("connection_tables", sa.Column("embedding_model", sa.String(), nullable=True))
    if not _has_column("connection_tables", "embedding_hash"):
        op.add_column("connection_tables", sa.Column("embedding_hash", sa.String(), nullable=True))


def downgrade() -> None:
    for col in ("embedding_hash", "embedding_model", "embedding"):
        if _has_column("connection_tables", col):
            op.drop_column("connection_tables", col)
