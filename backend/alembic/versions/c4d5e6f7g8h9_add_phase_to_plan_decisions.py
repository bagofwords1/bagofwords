"""add phase column to plan_decisions

Revision ID: c4d5e6f7g8h9
Revises: b3c4d5e6f7g8
Create Date: 2026-04-06 00:00:00.000000

Tags plan decisions with the phase they were produced in (e.g.
'knowledge_harness') so the harness can reuse the main loop's
persistence helpers while remaining distinguishable in the UI.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4d5e6f7g8h9'
down_revision: Union[str, None] = 'b3c4d5e6f7g8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('plan_decisions', sa.Column('phase', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('plan_decisions', 'phase')
