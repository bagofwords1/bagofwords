"""add report.last_activity_at for activity-based list sorting

Revision ID: la1a2b3c4d5e
Revises: c1d2e3f4a5b6
Create Date: 2026-06-28 10:00:00.000000

Adds `reports.last_activity_at`, a denormalized "last conversation activity"
timestamp used to sort the report list (sidebar + /reports) by real chat
activity instead of creation time.

It is bumped at two coarse choke points (see completion_service +
agent_v2): when a new user message is created and when an agent turn
finalizes. It is intentionally distinct from `updated_at`, which bumps on
any report-row metadata edit (rename / theme / sharing) and would conflate
"settings edited" with "conversation activity".

Existing rows are backfilled to the latest completion timestamp, falling
back to the report's own created_at when it has no completions. The backfill
is supported by the existing ix_completions_report_created index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'la1a2b3c4d5e'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'reports',
        sa.Column('last_activity_at', sa.DateTime(), nullable=True),
    )

    # Backfill from the latest completion, falling back to the report's own
    # created_at. Portable correlated subquery (works on Postgres + SQLite).
    op.execute(
        """
        UPDATE reports
        SET last_activity_at = COALESCE(
            (SELECT MAX(c.created_at) FROM completions c WHERE c.report_id = reports.id),
            reports.created_at
        )
        """
    )

    try:
        op.create_index('ix_reports_last_activity_at', 'reports', ['last_activity_at'])
    except Exception:
        # Index already exists, skip
        pass


def downgrade() -> None:
    try:
        op.drop_index('ix_reports_last_activity_at', table_name='reports')
    except Exception:
        pass
    op.drop_column('reports', 'last_activity_at')
