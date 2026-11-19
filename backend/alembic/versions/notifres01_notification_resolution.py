"""add notifications.resolved_at / resolution

Revision ID: notifres01
Revises: worldai01
Create Date: 2026-11-18 10:00:00.000000

A fanned-out review notification now mirrors its source ReviewItem closing:
when the item is resolved (or dismissed from the Review feed) every
recipient's notification records when and how, so the inbox shows it as
handled and the bell stops counting it. Both columns are nullable — existing
rows simply stay "open" — so there is no backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'notifres01'
down_revision: Union[str, Sequence[str], None] = 'worldai01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns('notifications')}


def upgrade() -> None:
    # Idempotent: the dashboards corpus runs schema-only revisions unstamped,
    # so a later `upgrade head` runs this again.
    cols = _columns()
    with op.batch_alter_table('notifications') as batch:
        if 'resolved_at' not in cols:
            batch.add_column(sa.Column('resolved_at', sa.DateTime(), nullable=True))
        if 'resolution' not in cols:
            batch.add_column(sa.Column('resolution', sa.String(16), nullable=True))


def downgrade() -> None:
    cols = _columns()
    with op.batch_alter_table('notifications') as batch:
        if 'resolution' in cols:
            batch.drop_column('resolution')
        if 'resolved_at' in cols:
            batch.drop_column('resolved_at')
