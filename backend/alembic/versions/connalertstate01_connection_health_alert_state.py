"""connection health alert bookkeeping: connections.health_alert_state

Revision ID: connalertstate01
Revises: connalertmode01
Create Date: 2026-11-05 00:00:00.000000

Additive only. Records the health state last announced for a shared
connection (NULL / "down" / "down_emailed"), so a down alert that a manual
"Test connection" pre-empted is still sent by the next automated probe, and a
recovery email goes out only when the matching down email did. Existing rows
keep NULL = nothing outstanding. Shared connections that are ALREADY inactive
are backfilled as "down" (announced) so deploying this does not fire a burst of
alerts for old outages; their recovery still lands in the inbox.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'connalertstate01'
down_revision: Union[str, None] = 'connalertmode01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if 'health_alert_state' not in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.add_column(sa.Column('health_alert_state', sa.String(length=16), nullable=True))
    connections = sa.table(
        'connections',
        sa.column('health_alert_state', sa.String),
        sa.column('is_active', sa.Boolean),
        sa.column('auth_policy', sa.String),
    )
    op.execute(
        connections.update()
        .where(connections.c.health_alert_state.is_(None))
        .where(connections.c.is_active == sa.false())
        .where(connections.c.auth_policy == 'system_only')
        .values(health_alert_state='down')
    )


def downgrade() -> None:
    if 'health_alert_state' in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.drop_column('health_alert_state')
