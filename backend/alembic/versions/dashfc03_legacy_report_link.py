"""Dashboards as first-class entities: where an old /r/{conversation} link leads

Revision ID: dashfc03
Revises: dashfc02
Create Date: 2026-10-12

Phase 4 of docs/design/dashboards-first-class.md. Links of the form
/r/{report_id} were sent before dashboards had links of their own, and each
opened whichever dashboard of the conversation had been edited last. Each
must keep leading to the dashboard it showed at the upgrade, forever:
`artifacts.legacy_report_id` names the conversation whose old link opens this
dashboard. Unique, so a conversation leads to one dashboard; no foreign key,
so the value outlives the conversation (a published dashboard survives its
conversation's deletion). The data migration fills it; until then it is empty
and /r behaves as before.

The downgrade drops the column. It refuses -- with a message, never by
deleting -- while a value is set, before any DDL.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc03"
down_revision: str | None = "dashfc02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("legacy_report_id", sa.String(length=36), nullable=True))
        batch.create_index("uq_artifacts_legacy_report_id", ["legacy_report_id"], unique=True)


def _refuse_if_a_link_is_recorded() -> None:
    from alembic import context

    if context.is_offline_mode():
        op.execute("-- dashfc03: check first that no dashboard records an old conversation link")
        return
    n = op.get_bind().execute(
        sa.text("SELECT count(*) FROM artifacts WHERE legacy_report_id IS NOT NULL")
    ).scalar() or 0
    if n:
        raise RuntimeError(
            f"dashfc03 downgrade refused: {n} dashboards record which old conversation link leads to them, "
            "and the previous schema cannot hold that. Nothing was changed. Restore the pre-upgrade backup "
            "instead, or clear these values deliberately first."
        )


def downgrade() -> None:
    _refuse_if_a_link_is_recorded()
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_index("uq_artifacts_legacy_report_id")
        batch.drop_column("legacy_report_id")
