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


def upgrade() -> None:
    op.add_column(
        'connections',
        sa.Column('consecutive_test_failures', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('connections', 'consecutive_test_failures')
