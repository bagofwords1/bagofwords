"""Custom tables: run history and incremental refresh settings

Revision ID: cqruns01
Revises: rel0615
Create Date: 2026-11-11

`custom_table_runs`: one row per refresh of a custom table — what started it,
the cursor range it covered, what it read and wrote, and a short event log.

`connection_tables` gains how a custom table refreshes: `refresh_mode`
('full' for every existing row, so nothing changes until an admin opts in),
`refresh_config` (cursor column, key, lookback, guards) and `refresh_state`
(the checkpoint and a first load in progress).

Additive only: no existing row changes and nothing is backfilled.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "cqruns01"
down_revision: str | None = "rel0615"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("connection_tables", "refresh_mode"):
        op.add_column("connection_tables",
                      sa.Column("refresh_mode", sa.String(), nullable=False, server_default="full"))
    if not _has_column("connection_tables", "refresh_config"):
        op.add_column("connection_tables", sa.Column("refresh_config", sa.JSON(), nullable=True))
    if not _has_column("connection_tables", "refresh_state"):
        op.add_column("connection_tables", sa.Column("refresh_state", sa.JSON(), nullable=True))

    if _has_table("custom_table_runs"):
        return
    op.create_table(
        "custom_table_runs",
        sa.Column("id", S36, nullable=False),
        sa.Column("connection_table_id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="full"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("triggered_by_user_id", S36, nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("window_from", sa.JSON(), nullable=True),
        sa.Column("window_to", sa.JSON(), nullable=True),
        sa.Column("rows_read", sa.BigInteger(), nullable=True),
        sa.Column("rows_inserted", sa.BigInteger(), nullable=True),
        sa.Column("rows_updated", sa.BigInteger(), nullable=True),
        sa.Column("rows_deleted", sa.BigInteger(), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=True),
        sa.Column("rendered_query", sa.Text(), nullable=True),
        sa.Column("schema_fingerprint", sa.String(length=32), nullable=True),
        sa.Column("inputs", sa.JSON(), nullable=True),
        sa.Column("chunks", sa.JSON(), nullable=True),
        sa.Column("events", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_custom_table_runs"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                name="fk_custom_table_runs_organization_id"),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["users.id"],
                                name="fk_custom_table_runs_triggered_by_user_id", ondelete="SET NULL"),
    )
    op.create_index("ix_custom_table_runs_table_started", "custom_table_runs",
                    ["connection_table_id", "started_at"])


def downgrade() -> None:
    if _has_table("custom_table_runs"):
        op.drop_index("ix_custom_table_runs_table_started", table_name="custom_table_runs")
        op.drop_table("custom_table_runs")
    for column in ("refresh_state", "refresh_config", "refresh_mode"):
        if _has_column("connection_tables", column):
            op.drop_column("connection_tables", column)
