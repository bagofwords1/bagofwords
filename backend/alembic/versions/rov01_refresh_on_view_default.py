"""Refresh-on-view becomes the default.

Artifacts should open on fresh data: new reports get refresh_on_view=1, and
existing reports without a recurring schedule are flipped on (a report with a
cron schedule keeps its mode — the UI treats the two as exclusive, and a
schedule is the stronger, deliberately-configured choice). The server-side
staleness gate + single-flight claim in refresh_on_view_rerun remain the rate
ceiling, so this changes freshness, not load characteristics per interval.

Revision ID: rov01
Revises: qparams02
"""
import sqlalchemy as sa
from alembic import op

revision = "rov01"
down_revision = "qparams02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reports") as batch_op:
        batch_op.alter_column(
            "refresh_on_view",
            existing_type=sa.Boolean(),
            server_default="1",
            existing_nullable=False,
        )
    op.execute(
        "UPDATE reports SET refresh_on_view = 1 "
        "WHERE refresh_on_view = 0 "
        "AND (cron_schedule IS NULL OR cron_schedule = '' OR cron_schedule = 'None')"
    )


def downgrade() -> None:
    # The data flip is not reversible (rows that were already on are
    # indistinguishable) — only the default reverts.
    with op.batch_alter_table("reports") as batch_op:
        batch_op.alter_column(
            "refresh_on_view",
            existing_type=sa.Boolean(),
            server_default="0",
            existing_nullable=False,
        )
