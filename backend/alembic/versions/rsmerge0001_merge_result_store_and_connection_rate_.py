"""merge result store and connection rate limit heads

Revision ID: rsmerge0001
Revises: art1f4c70001, connratelimit01
Create Date: 2026-07-10 18:01:36.949468

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'rsmerge0001'
down_revision: Union[str, None] = ('art1f4c70001', 'connratelimit01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
