"""file preview

Revision ID: e7769f2fca82
Revises: 97757b1ff76f
Create Date: 2025-12-04 20:08:33.561106

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7769f2fca82'
down_revision: Union[str, None] = '97757b1ff76f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('files', sa.Column('preview', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('files', 'preview')
