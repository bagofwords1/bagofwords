"""Custom tables: one materialized copy per user on per-user connections

Revision ID: ucopy01
Revises: mrgheads12
Create Date: 2026-11-27

`custom_table_user_copies`: on a connection where each person queries with
their own credentials, a custom table is materialized once per user, with
that user's credentials, so the source's row-level security decides what each
copy holds. The table's definition stays on `connection_tables`.

Additive only: no existing row changes and nothing is backfilled.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ucopy01"
down_revision: str | None = "mrgheads12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)
TABLE = "custom_table_user_copies"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", S36, nullable=False),
        sa.Column("connection_table_id", S36, nullable=False),
        sa.Column("connection_id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("user_id", S36, nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="building"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("artifact_path", sa.String(), nullable=True),
        sa.Column("artifact_key_enc", sa.Text(), nullable=True),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("columns", sa.JSON(), nullable=True),
        sa.Column("definition_hash", sa.String(length=64), nullable=True),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("failed_definition_hash", sa.String(length=64), nullable=True),
        sa.Column("failed_credential_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("build_ms", sa.Integer(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("building_since", sa.DateTime(), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        sa.Column("refresh_requested_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_custom_table_user_copies"),
        sa.ForeignKeyConstraint(["connection_table_id"], ["connection_tables.id"],
                                name="fk_custom_table_user_copies_connection_table_id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                name="fk_custom_table_user_copies_organization_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_custom_table_user_copies_user_id"),
        sa.UniqueConstraint("connection_table_id", "user_id",
                            name="uq_custom_table_user_copies_table_user"),
    )
    op.create_index("ix_custom_table_user_copies_user", TABLE, ["user_id"])
    op.create_index("ix_custom_table_user_copies_connection", TABLE, ["connection_id"])


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    op.drop_index("ix_custom_table_user_copies_connection", table_name=TABLE)
    op.drop_index("ix_custom_table_user_copies_user", table_name=TABLE)
    op.drop_table(TABLE)
