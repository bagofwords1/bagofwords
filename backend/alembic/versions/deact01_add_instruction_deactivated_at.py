"""add deactivated_at to instructions

Revision ID: deact01
Revises: ldapsecurity01
Create Date: 2026-09-07 00:00:00.000000

status='draft' is overloaded: a new suggestion awaiting review and a published
instruction the user switched OFF both carry it. deactivated_at durably records
the second meaning (set on a user-driven published→draft transition, cleared on
re-publish) so session-scoped surfaces such as search_instructions' own-drafts
widening can keep switched-off instructions out of the agent's view.

No backfill: historical rows carry no record of *why* they are draft, so they
are left NULL (treated as awaiting review — the safe, visible default).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'deact01'
down_revision: Union[str, None] = 'ldapsecurity01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instructions', sa.Column('deactivated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('instructions', 'deactivated_at')
