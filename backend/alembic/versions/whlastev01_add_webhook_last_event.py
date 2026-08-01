"""add last_event capture to webhooks

Revision ID: whlastev01
Revises: sptitle01
Create Date: 2026-08-01 10:00:00.000000

Stores the most recent verified delivery on the trigger itself so the setup UI
can confirm "your event arrived" before the trigger is activated, and so the
owner has a real sample payload to write the task against. Auth-bearing headers
are stripped before storage.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'whlastev01'
down_revision: Union[str, None] = 'sptitle01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('webhooks', sa.Column('last_event', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('webhooks', 'last_event')
