"""overlay FKs: ON DELETE CASCADE / SET NULL

Per-user overlay rows (user_data_source_tables/columns, user_connection_tables/
columns) referenced their parent rows with no ondelete action, so deleting a
connection/data source (which cascades to datasource_tables / connection_tables)
hit a ForeignKeyViolationError. Add:
  - CASCADE on the owning-entity FKs (data_source/connection, overlay-table→column)
  - SET NULL on the soft table-link FKs (nullable; preserves per-user audit history
    across schema re-syncs that drop a canonical table)

Revision ID: overlay_fk_ondelete
Revises: f9a0b1c2d3e4
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'overlay_fk_ondelete'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, referred_table, referred_col, ondelete)
_FKS = [
    ("user_data_source_tables", "data_source_id", "data_sources", "id", "CASCADE"),
    ("user_data_source_tables", "data_source_table_id", "datasource_tables", "id", "SET NULL"),
    ("user_data_source_columns", "user_data_source_table_id", "user_data_source_tables", "id", "CASCADE"),
    ("user_connection_tables", "connection_id", "connections", "id", "CASCADE"),
    ("user_connection_tables", "connection_table_id", "connection_tables", "id", "SET NULL"),
    ("user_connection_columns", "user_connection_table_id", "user_connection_tables", "id", "CASCADE"),
]

# Deterministic FK naming so batch-mode reflection (SQLite, where the original
# FKs are anonymous) can locate and drop them.
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _conv_name(table: str, column: str, referred: str) -> str:
    return f"fk_{table}_{column}_{referred}"


def _recreate(use_ondelete: bool) -> None:
    """Drop and recreate every overlay FK; apply its ondelete action when
    use_ondelete is True (upgrade), or no action (downgrade)."""
    dialect = op.get_bind().dialect.name
    for table, column, ref_t, ref_c, action in _FKS:
        ondelete = action if use_ondelete else None
        if dialect == "sqlite":
            with op.batch_alter_table(table, naming_convention=_NAMING) as batch_op:
                batch_op.drop_constraint(_conv_name(table, column, ref_t), type_="foreignkey")
                batch_op.create_foreign_key(
                    _conv_name(table, column, ref_t), ref_t, [column], [ref_c],
                    ondelete=ondelete,
                )
        else:
            op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
            op.create_foreign_key(
                f"{table}_{column}_fkey", table, ref_t, [column], [ref_c],
                ondelete=ondelete,
            )


def upgrade() -> None:
    _recreate(use_ondelete=True)


def downgrade() -> None:
    _recreate(use_ondelete=False)
