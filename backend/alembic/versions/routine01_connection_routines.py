"""Stored procedures / functions discovered from a connection

Revision ID: routine01
Revises: refhist01
Create Date: 2026-10-29

`connection_routines`: one row per stored procedure / function introspected
from a SQL connection's catalog. Reference material only -- name, type and
signature, never the body (the source text is fetched live on demand). Additive
only: no existing row changes, nothing is backfilled.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "routine01"
down_revision: str | None = "refhist01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("connection_routines"):
        return
    op.create_table(
        "connection_routines",
        sa.Column("id", S36, nullable=False),
        sa.Column("connection_id", S36, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("schema_name", sa.String(), nullable=True),
        sa.Column("routine_type", sa.String(), nullable=False, server_default="procedure"),
        sa.Column("language", sa.String(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("arguments", sa.Text(), nullable=True),
        sa.Column("returns", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("definition_available", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_connection_routines"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"],
                                name="fk_connection_routines_connection_id", ondelete="CASCADE"),
        sa.UniqueConstraint("connection_id", "name", name="uq_connection_routines_conn_name"),
    )
    op.create_index("ix_connection_routines_connection_id", "connection_routines", ["connection_id"])


def downgrade() -> None:
    if not _has_table("connection_routines"):
        return
    op.drop_index("ix_connection_routines_connection_id", table_name="connection_routines")
    op.drop_table("connection_routines")
