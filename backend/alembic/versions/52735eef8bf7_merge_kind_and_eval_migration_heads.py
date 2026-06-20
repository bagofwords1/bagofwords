"""merge kind and eval migration heads

Revision ID: 52735eef8bf7
Revises: c4a1d2e3f4b5, rel1auto2mation
Create Date: 2026-06-15 12:43:57.313443

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '52735eef8bf7'
down_revision: Union[str, None] = ('c4a1d2e3f4b5', 'rel1auto2mation')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
