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

The downgrade has to put the narrower key back over rows the new key made
legal, so it reconciles cross-connection duplicates first — see
``_collapse_cross_connection_duplicates``.

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


def _udst_tables():
    """Lightweight table clauses for the reconciliation DML.

    Expressed in Core rather than raw SQL so booleans render correctly on both
    PostgreSQL (``true``) and SQLite (``1``).
    """
    udst = sa.table(
        _TABLE,
        sa.column("id", sa.String),
        sa.column("data_source_id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("table_name", sa.String),
        sa.column("is_accessible", sa.Boolean),
    )
    udsc = sa.table(
        "user_data_source_columns",
        sa.column("id", sa.String),
        sa.column("user_data_source_table_id", sa.String),
    )
    return udst, udsc


def _delete_overlay_rows(udst, udsc, where_clause) -> None:
    """Delete overlay rows and their columns.

    The columns' FK cascades on PostgreSQL, but SQLite does not enforce foreign
    keys unless the connection asked it to, so the children go first explicitly.
    """
    doomed = sa.select(udst.c.id).where(where_clause)
    op.execute(udsc.delete().where(udsc.c.user_data_source_table_id.in_(doomed)))
    op.execute(udst.delete().where(udst.c.id.in_(doomed)))


def _collapse_cross_connection_duplicates() -> None:
    """Reduce each (data source, user, table name) to ONE row.

    Per-connection overlays make ``(data_source_id, user_id, table_name)``
    legitimately non-unique: two connections on one agent can each expose an
    ``orders``. The old constraint cannot be recreated over those rows — the
    downgrade failed outright with a unique violation on any agent that had
    started using the feature — so the rows are reconciled first.

    The downgrade is lossy by nature (the old schema has nowhere to record which
    connection a row described). The policy keeps the most permissive answer,
    which is the one the pre-connection readers assumed: an accessible row wins
    over an inaccessible one for the same name, and among equals the lowest id
    wins so the outcome is deterministic and repeatable.
    """
    udst, udsc = _udst_tables()

    # 1. Drop inaccessible rows whose name is accessible on another connection.
    v = udst.alias("v")
    _delete_overlay_rows(
        udst, udsc,
        sa.and_(
            udst.c.is_accessible.is_(False),
            sa.exists(
                sa.select(sa.literal(1)).select_from(v).where(
                    v.c.data_source_id == udst.c.data_source_id,
                    v.c.user_id == udst.c.user_id,
                    v.c.table_name == udst.c.table_name,
                    v.c.is_accessible.is_(True),
                )
            ),
        ),
    )

    # 2. Collapse whatever duplicates remain (same accessibility) to one row.
    keep = sa.select(sa.func.min(udst.c.id)).group_by(
        udst.c.data_source_id, udst.c.user_id, udst.c.table_name
    )
    _delete_overlay_rows(udst, udsc, udst.c.id.notin_(keep))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    _collapse_cross_connection_duplicates()
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
