"""add show_widget_info to reports and artifacts

Revision ID: shwinfo01
Revises: cqruns01
Create Date: 2026-11-11 00:00:00.000000

The per-object provenance popover (the ⓘ on each dashboard widget, showing its
Data / Code / Calculation) is now a per-dashboard setting rather than always on.
Mirrors include_data_tab: the flag lives on both ``reports`` (the conversation's
answer / legacy /r) and ``artifacts`` (the first-class dashboard the /d and /r
pages read).

Defaults to FALSE — the popover is off for every existing dashboard; an owner
turns it back on per dashboard in settings. Display-only, like the Data tab.

Additive and idempotent (the dashboard test corpus applies it unstamped,
tests/fixtures/dashboard_corpus/alembic_at.py).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "shwinfo01"
down_revision: str | None = "cqruns01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str) -> bool:
    return "show_widget_info" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Native ADD/DROP COLUMN, not batch_alter_table (as dashlist01): on SQLite
    # batch mode can rebuild the table -- always for a drop, and for an add
    # whose default is a SQL expression such as sa.false() -- and with foreign
    # keys on, dropping the old table cascades into every child
    # (artifact_host_applications, ...). A constant default keeps it one ALTER.
    for table in ("reports", "artifacts"):
        if _has_column(table):
            continue
        op.add_column(table, sa.Column("show_widget_info", sa.Boolean(), nullable=False,
                                       server_default="0"))


def downgrade() -> None:
    for table in ("artifacts", "reports"):
        if not _has_column(table):
            continue
        op.drop_column(table, "show_widget_info")
