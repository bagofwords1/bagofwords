"""merge release 0.0.614 and data source kind heads

Revision ID: rel0615
Revises: rel0614, dskind01
Create Date: 2026-11-10 21:30:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'rel0615'
down_revision: Union[str, None] = ('rel0614', 'dskind01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
