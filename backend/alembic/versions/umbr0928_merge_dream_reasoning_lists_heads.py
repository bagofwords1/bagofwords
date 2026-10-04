"""merge overnight learning, reasoning effort and agent lists heads

Revision ID: umbr0928
Revises: dream01, reasoneffort01, agentlists01
Create Date: 2026-09-28 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'umbr0928'
down_revision: Union[str, None] = ('dream01', 'reasoneffort01', 'agentlists01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
