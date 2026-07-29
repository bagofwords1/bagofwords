"""merge svr0001 (viewer-run) with main

Revision ID: 2a48698d312e
Revises: mrgheads03, svr0001
Create Date: 2026-07-27 03:59:38.250295

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a48698d312e'
down_revision: Union[str, None] = ('mrgheads03', 'svr0001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
