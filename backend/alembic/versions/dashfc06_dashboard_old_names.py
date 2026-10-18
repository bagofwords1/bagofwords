"""Dashboards as first-class entities: a dashboard's old names

Revision ID: dashfc06
Revises: dashfc05
Create Date: 2026-10-18

Phase 8 of docs/design/dashboards-first-class.md. The owner names a
dashboard (`artifacts.slug`, from dashfc01) and `/d/{name}` opens it. A
renamed dashboard keeps its old names: each row is a name it had, unique
in the installation like the current names, leading to the dashboard
until the owner removes the name (`released_at`) -- and never taken by
another dashboard.

The downgrade drops the table: old names stop leading anywhere.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc06"
down_revision: str | None = "dashfc05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def upgrade() -> None:
    op.create_table(
        "artifact_slug_history",
        sa.Column("id", S36, nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_slug_history"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"],
                                name="fk_artifact_slug_history_artifact_id", ondelete="CASCADE"),
        sa.UniqueConstraint("slug", name="uq_artifact_slug_history_slug"),
    )
    op.create_index("ix_artifact_slug_history_artifact_id", "artifact_slug_history", ["artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_slug_history_artifact_id", table_name="artifact_slug_history")
    op.drop_table("artifact_slug_history")
