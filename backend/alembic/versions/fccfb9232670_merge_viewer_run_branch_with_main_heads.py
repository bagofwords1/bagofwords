"""merge viewer-run branch with main heads

Revision ID: fccfb9232670
Revises: 573be7e86930, fastq002
Create Date: 2026-07-29 10:22:21.926296

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fccfb9232670'
down_revision: Union[str, None] = ('573be7e86930', 'fastq002')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
