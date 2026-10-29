"""merge agent check-ins and user memory heads

Revision ID: mrgckmem01
Revises: agentcheckins02, usrmem01
Create Date: 2026-09-28 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mrgckmem01'
down_revision: Union[str, None] = ('agentcheckins02', 'usrmem01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
