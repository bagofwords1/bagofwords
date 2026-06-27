"""merge prompt + notifications heads

Revision ID: 6d171618ced8
Revises: prompt0001, notif0001
Create Date: 2026-06-27 07:33:16.675081

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6d171618ced8'
down_revision: Union[str, None] = ('prompt0001', 'notif0001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
