"""merge share groups and eval fixtures heads

Revision ID: 297905a87c8a
Revises: rsgrp0001, evalfix001
Create Date: 2026-07-31 19:06:59.936360

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '297905a87c8a'
down_revision: Union[str, None] = ('rsgrp0001', 'evalfix001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
