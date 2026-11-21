"""Tool confirmations: a separate ``display`` column for card-only context.

The approval card showed display context (the agent a call goes through, a
preview, the effect) as underscore keys inside ``arguments`` and hid every
underscore key — which also hid real provider arguments such as ``_id``.
Display context now has its own column; ``arguments`` holds what the tool gets.

Downgrade drops the column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'tconfdisp01'
down_revision: Union[str, None] = 'connact01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tool_confirmations') as batch:
        batch.add_column(sa.Column('display', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tool_confirmations') as batch:
        batch.drop_column('display')
