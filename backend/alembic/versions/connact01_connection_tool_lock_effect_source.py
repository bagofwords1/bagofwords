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


def upgrade() -> None:
    with op.batch_alter_table('connection_tools') as batch:
        batch.add_column(sa.Column('policy_locked', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('effect', sa.String(), nullable=True))
        batch.add_column(sa.Column('source', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('connection_tools') as batch:
        batch.drop_column('source')
        batch.drop_column('effect')
        batch.drop_column('policy_locked')
