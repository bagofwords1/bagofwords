"""merge heads after repair

Revision ID: fb664b5328e0
Revises: fa82623106ce, 7e96b24b33ad
Create Date: 2025-11-16 12:46:36.206279

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb664b5328e0'
down_revision: Union[str, None] = ('fa82623106ce', '7e96b24b33ad')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
