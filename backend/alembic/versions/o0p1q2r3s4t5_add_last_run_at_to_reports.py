"""add last_run_at to reports

Revision ID: o0p1q2r3s4t5
Revises: n9o0p1q2r3s4
Create Date: 2025-01-24 10:00:00.000000

Adds last_run_at column to reports table to track when a report was last run/refreshed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'o0p1q2r3s4t5'
down_revision: Union[str, None] = 'n9o0p1q2r3s4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reports', sa.Column('last_run_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('reports', 'last_run_at')
