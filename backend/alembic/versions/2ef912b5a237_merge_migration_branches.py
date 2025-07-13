"""merge migration branches

Revision ID: 2ef912b5a237
Revises: ef9ca851f6ea, 5a283be8a36f
Create Date: 2025-07-13 18:05:39.062545

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ef912b5a237'
down_revision: Union[str, None] = ('ef9ca851f6ea', '5a283be8a36f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
