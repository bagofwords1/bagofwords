"""Dashboards as first-class entities: a query no longer needs a widget

Revision ID: dashfc02
Revises: dashfc01
Create Date: 2026-10-11

Phase 2 of docs/design/dashboards-first-class.md. A published dashboard's
copies of queries belong to their release (`artifact_release_id`) and have no
conversation, while a widget always belongs to one -- so a copy has no
widget. `queries.widget_id` becomes nullable; `steps.widget_id` already is in
the database. Every existing query keeps its widget, and the code that creates
a conversation's queries still creates one.

The same release ships the code that refuses, on every conversation path, a
query or step that has no conversation, so no row without a widget is ever
read the old way.

The downgrade restores NOT NULL. It refuses -- with a message, never by
deleting -- while a query without a widget exists, before any DDL.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc02"
down_revision: str | None = "dashfc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def upgrade() -> None:
    with op.batch_alter_table("queries") as batch:
        batch.alter_column("widget_id", existing_type=S36, nullable=True)


def _refuse_if_a_query_has_no_widget() -> None:
    from alembic import context

    if context.is_offline_mode():
        op.execute("-- dashfc02: check first that every query has a widget")
        return
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM queries WHERE widget_id IS NULL")).scalar() or 0
    if n:
        raise RuntimeError(
            f"dashfc02 downgrade refused: {n} queries have no widget (published dashboards' copies), "
            "and the previous schema requires one. Nothing was changed. Restore the pre-upgrade backup "
            "instead, or remove these queries deliberately first."
        )


def downgrade() -> None:
    _refuse_if_a_query_has_no_widget()
    with op.batch_alter_table("queries") as batch:
        batch.alter_column("widget_id", existing_type=S36, nullable=False)
