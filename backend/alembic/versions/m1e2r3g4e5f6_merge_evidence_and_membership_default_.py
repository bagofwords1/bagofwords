"""merge evidence and membership-default-llm heads

Revision ID: m1e2r3g4e5f6
Revises: e7f8a9b0c1d2, ud1a2b3c4d5e
Create Date: 2026-07-09 05:48:01.369745

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'm1e2r3g4e5f6'
down_revision: Union[str, None] = ('e7f8a9b0c1d2', 'ud1a2b3c4d5e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
