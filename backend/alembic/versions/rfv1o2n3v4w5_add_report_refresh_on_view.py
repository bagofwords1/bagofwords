"""add refresh_on_view to reports

Revision ID: rfv1o2n3v4w5
Revises: mrgheads03
Create Date: 2026-07-27 00:00:00.000000

Opt-in flag: when set, opening the shared report page (/r/{id}) reruns the
artifact's queries before rendering. Defaults to false so existing reports are
unaffected. The refresh interval is not stored — it is a hardcoded server-side
constant, which is what bounds how often a rerun can actually fire.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'rfv1o2n3v4w5'
down_revision: Union[str, None] = 'mrgheads03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'reports',
        sa.Column('refresh_on_view', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('reports', 'refresh_on_view')
