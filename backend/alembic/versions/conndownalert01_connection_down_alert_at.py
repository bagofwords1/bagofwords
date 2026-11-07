"""connection down-alert throttle: connections.last_down_alert_at

Revision ID: conndownalert01
Revises: auditcontract01
Create Date: 2026-11-05 00:00:00.000000

Additive only. One nullable timestamp stamped when a "connection is down"
alert email last went out for a shared connection, so a flapping connection's
email is throttled to at most one per cooldown window. The in-app inbox row is
unaffected (it dedups by group_key). Existing rows keep NULL = never alerted.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'conndownalert01'
down_revision: Union[str, None] = 'auditcontract01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent: the dashboard corpus fixture replays schema-only revisions
    # unstamped, then `upgrade head` must find the work done.
    if 'last_down_alert_at' not in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.add_column(sa.Column('last_down_alert_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    if 'last_down_alert_at' in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.drop_column('last_down_alert_at')
