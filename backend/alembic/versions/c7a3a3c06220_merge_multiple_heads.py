"""merge multiple heads

Revision ID: c7a3a3c06220
Revises: 4e6982618f6e, c76b76a7869a
Create Date: 2025-07-16 11:52:34.128120

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7a3a3c06220'
down_revision: Union[str, None] = ('4e6982618f6e', 'c76b76a7869a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
