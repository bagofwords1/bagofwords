"""Index the history columns that reference a catalog row

Revision ID: dstblref01
Revises: mrgheads12
Create Date: 2026-11-26

Deleting an agent deletes its catalog rows (datasource_tables: one per file on
a file agent, thousands of them). For each deleted row Postgres checks every
column that references datasource_tables.id. Two of them had no index:
table_usage_events.datasource_table_id and table_feedback_events.datasource_table_id.
Each check then read the whole table, so on an install with a long usage
history deleting a file agent took minutes. Indexed here.

The indexes are partial (rows that point at a catalog row) and, on Postgres,
built CONCURRENTLY: table_usage_events is written on every query, and a plain
CREATE INDEX would block those writes for the whole build. CONCURRENTLY cannot
run inside a transaction, hence the autocommit block. A build that was
interrupted leaves an INVALID index of the same name, which IF NOT EXISTS
would skip, so any such leftover is dropped first.

The downgrade drops the indexes; no data changes.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dstblref01"
down_revision: str | None = "mrgheads12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index, table): the columns that reference datasource_tables.id without one.
_CATALOG_ROW_REFERENCES = (
    ("ix_table_usage_events_datasource_table_id", "table_usage_events"),
    ("ix_table_feedback_events_datasource_table_id", "table_feedback_events"),
)
_COLUMN = "datasource_table_id"
_WHERE = f"{_COLUMN} IS NOT NULL"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        for name, table in _CATALOG_ROW_REFERENCES:
            op.create_index(name, table, [_COLUMN], if_not_exists=True, sqlite_where=sa.text(_WHERE))
        return

    with op.get_context().autocommit_block():
        for name, table in _CATALOG_ROW_REFERENCES:
            invalid = op.get_bind().execute(sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ), {"name": name}).first()
            if invalid:
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
            op.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" ON "{table}" ("{_COLUMN}") WHERE {_WHERE}'
            )


def downgrade() -> None:
    if not _is_postgres():
        for name, table in reversed(_CATALOG_ROW_REFERENCES):
            op.drop_index(name, table_name=table, if_exists=True)
        return

    with op.get_context().autocommit_block():
        for name, _ in reversed(_CATALOG_ROW_REFERENCES):
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
