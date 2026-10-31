"""Which outside application a conversation or agent run came through

Revision ID: origin01
Revises: credit01
Create Date: 2026-10-30

* ``reports.origin_app_type`` / ``origin_app_id`` and the same on
  ``agent_executions``: a host application (embedded artifact) or an OAuth
  app (MCP client). Null: made in BOW itself. See app/core/request_origin.py.

Additive and idempotent (the dashboard test corpus applies it unstamped,
tests/fixtures/dashboard_corpus/alembic_at.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "origin01"
down_revision: str | None = "credit01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("reports", "agent_executions")


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table in TABLES:
        if "origin_app_type" in _columns(table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("origin_app_type", sa.String(length=16), nullable=True))
            batch.add_column(sa.Column("origin_app_id", sa.String(length=36), nullable=True))
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("reports")}
    if "ix_reports_origin_app_id" not in indexes:
        op.create_index("ix_reports_origin_app_id", "reports", ["origin_app_id"])


def downgrade() -> None:
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("reports")}
    if "ix_reports_origin_app_id" in indexes:
        op.drop_index("ix_reports_origin_app_id", table_name="reports")
    for table in TABLES:
        if "origin_app_type" not in _columns(table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_column("origin_app_id")
            batch.drop_column("origin_app_type")
