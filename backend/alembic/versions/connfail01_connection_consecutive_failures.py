"""add connections.consecutive_test_failures

Revision ID: connfail01
Revises: refhist01
Create Date: 2026-10-29 10:00:00.000000

Counts health-probe failures in a row so one slow or failed probe no longer
deactivates a shared connection (which hides its tables from every agent).
A connection is deactivated only once the count reaches the threshold; any
successful probe resets it to 0.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'connfail01'
down_revision: Union[str, None] = 'refhist01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent, as collab01 and refhist01: the dashboard corpus stops before
    # dashfc08's data migration and applies this schema-only revision on its
    # own; a later `upgrade head` then finds it done.
    if _has_column('connections', 'consecutive_test_failures'):
        return
    op.add_column(
        'connections',
        sa.Column('consecutive_test_failures', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    if not _has_column('connections', 'consecutive_test_failures'):
        return
    op.drop_column('connections', 'consecutive_test_failures')
