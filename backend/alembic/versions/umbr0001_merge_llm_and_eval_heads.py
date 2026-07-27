"""merge llm context-window and eval-wake migration heads

Revision ID: umbr0001
Revises: c1w2o3v4r5d6, evalwake0002
Create Date: 2026-07-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'umbr0001'
down_revision: Union[str, None] = ('c1w2o3v4r5d6', 'evalwake0002')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
