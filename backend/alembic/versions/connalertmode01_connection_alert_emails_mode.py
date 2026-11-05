"""per-connection alert email switch: connections.alert_emails_enabled

Revision ID: connalertmode01
Revises: conndownalert01
Create Date: 2026-11-05 00:00:00.000000

Additive only. A per-connection on/off switch for down/restored alert emails,
gated UNDER the org-wide toggle: the effective send = org toggle AND this
switch. When the org toggle is off, nothing sends and this switch is disabled
in the UI. Existing rows default to True (send when the org toggle is on),
preserving today's behavior. The in-app inbox notification is unaffected.
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
    if 'alert_emails_enabled' not in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'alert_emails_enabled', sa.Boolean(),
                nullable=False, server_default=sa.true(),
            ))


def downgrade() -> None:
    if 'alert_emails_enabled' in _columns('connections'):
        with op.batch_alter_table('connections', schema=None) as batch_op:
            batch_op.drop_column('alert_emails_enabled')
