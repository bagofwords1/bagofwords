"""Make the per-user table overlay connection-aware.

``user_data_source_tables`` recorded which tables a given user may see on a
given DATA SOURCE. An agent, however, can hold several connections, and the
per-user catalog is a property of a connection (its auth policy, its token),
not of the agent. Two consequences, both user-visible:

* the unique key ``(data_source_id, user_id, table_name)`` collided whenever
  two connections on one agent exposed the same table name, so the second
  connection's accessibility overwrote the first's;
* readers could not tell which connection an overlay row described, so the
  tables selector filtered the WHOLE catalog through an overlay that only ever
  described one connection and every other connection's tables disappeared.

This adds ``connection_id`` and swaps the unique constraint. The backfill
resolves the connection through ``datasource_tables -> connection_tables``;
rows whose canonical table is unlinked (a delegated user's own discovery) stay
NULL and are treated by readers as "this user, any connection" — safe, because
every overlay read is already filtered by ``user_id``.

Revision ID: overlayconn01
Revises: diagrollup01
Create Date: 2026-09-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "overlayconn01"
down_revision: Union[str, None] = "diagrollup01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "user_data_source_tables"
_OLD_UQ = "uq_user_ds_table"
_NEW_UQ = "uq_user_ds_conn_table"
_IX = "ix_udst_ds_user_conn"
_IX_CONN = "ix_user_data_source_tables_connection_id"


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _constraint_names(bind, table: str) -> set:
    insp = sa.inspect(bind)
    names = {c.get("name") for c in insp.get_unique_constraints(table)}
    names |= {i.get("name") for i in insp.get_indexes(table)}
    return {n for n in names if n}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return

    # SQLite cannot ALTER a constraint in place; batch_alter_table rebuilds the
    # table. On PostgreSQL the same operations run as plain DDL.
    with op.batch_alter_table(_TABLE) as batch:
        if not _has_column(bind, _TABLE, "connection_id"):
            batch.add_column(sa.Column(
                "connection_id",
                sa.String(36),
                sa.ForeignKey(
                    "connections.id",
                    ondelete="CASCADE",
                    # SQLite rebuilds the table in batch mode and refuses to copy
                    # an unnamed constraint, so name it explicitly.
                    name="fk_user_data_source_tables_connection_id_connections",
                ),
                nullable=True,
            ))

    # Backfill before swapping the constraint so the new key is already correct.
    op.execute(
        sa.text(
            f"""
            UPDATE {_TABLE}
               SET connection_id = (
                   SELECT ct.connection_id
                     FROM datasource_tables dst
                     JOIN connection_tables ct ON ct.id = dst.connection_table_id
                    WHERE dst.id = {_TABLE}.data_source_table_id
               )
             WHERE connection_id IS NULL
               AND data_source_table_id IS NOT NULL
            """
        )
    )

    existing = _constraint_names(bind, _TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        if _OLD_UQ in existing:
            batch.drop_constraint(_OLD_UQ, type_="unique")
        if _NEW_UQ not in existing:
            batch.create_unique_constraint(
                _NEW_UQ, ["data_source_id", "user_id", "connection_id", "table_name"]
            )
        if _IX not in existing:
            batch.create_index(_IX, ["data_source_id", "user_id", "connection_id"])
        if _IX_CONN not in existing:
            batch.create_index(_IX_CONN, ["connection_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    existing = _constraint_names(bind, _TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        if _IX_CONN in existing:
            batch.drop_index(_IX_CONN)
        if _IX in existing:
            batch.drop_index(_IX)
        if _NEW_UQ in existing:
            batch.drop_constraint(_NEW_UQ, type_="unique")
        if _OLD_UQ not in existing:
            batch.create_unique_constraint(
                _OLD_UQ, ["data_source_id", "user_id", "table_name"]
            )
        if _has_column(bind, _TABLE, "connection_id"):
            batch.drop_column("connection_id")
