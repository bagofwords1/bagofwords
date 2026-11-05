"""per-connection alert email override: connections.alert_emails_mode

Revision ID: connalertmode01
Revises: conndownalert01
Create Date: 2026-11-05 00:00:00.000000

Additive only. A per-connection override layered over the org-wide
connection-alert email toggle: "inherit" (default, follow the org), "always"
(email even when the org toggle is off), "never" (mute this connection even
when the org toggle is on). Existing rows default to "inherit", preserving
today's behavior. The in-app inbox notification is unaffected.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'connalertmode01'
down_revision: Union[str, None] = 'conndownalert01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if 'alert_emails_mode' not in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'alert_emails_mode', sa.String(),
                nullable=False, server_default='inherit',
            ))


def downgrade() -> None:
    if 'alert_emails_mode' in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.drop_column('alert_emails_mode')
