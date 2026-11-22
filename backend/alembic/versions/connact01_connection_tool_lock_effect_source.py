"""Connection tools: policy lock, effect and source.

- ``policy_locked``: the connection admin's policy can be made stricter by an
  agent or a user, never looser. Existing rows are unlocked (no behaviour change).
- ``effect``: read | modify | external | destructive (NULL = unknown).
- ``source``: mcp | custom_api | remote | native (NULL = unknown).

Downgrade drops the three columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'connact01'
down_revision: Union[str, None] = 'rel0629'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMNS = ('policy_locked', 'effect', 'source')


def _columns() -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns('connection_tools')}


def upgrade() -> None:
    # Idempotent: the dashboard test corpus replays it unstamped
    # (tests/fixtures/dashboard_corpus/alembic_at.py), then `upgrade head`.
    have = _columns()
    with op.batch_alter_table('connection_tools') as batch:
        if 'policy_locked' not in have:
            batch.add_column(sa.Column('policy_locked', sa.Boolean(), nullable=False, server_default=sa.false()))
        if 'effect' not in have:
            batch.add_column(sa.Column('effect', sa.String(), nullable=True))
        if 'source' not in have:
            batch.add_column(sa.Column('source', sa.String(), nullable=True))


def downgrade() -> None:
    have = _columns()
    with op.batch_alter_table('connection_tools') as batch:
        for column in reversed(COLUMNS):
            if column in have:
                batch.drop_column(column)
