"""merge viewer-run branch with main (refresh_on_view)

Revision ID: 573be7e86930
Revises: 2a48698d312e, rfv1o2n3v4w5
Create Date: 2026-07-27 17:41:49.977802

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '573be7e86930'
down_revision: Union[str, None] = ('2a48698d312e', 'rfv1o2n3v4w5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
