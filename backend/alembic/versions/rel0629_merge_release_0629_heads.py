"""merge release 0.0.629 heads (notification resolution, steering stamp, exposed agents)

Revision ID: rel0629
Revises: notifres01, steerat01, exposed01
Create Date: 2026-11-18 20:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'rel0629'
down_revision: Union[str, None] = ('notifres01', 'steerat01', 'exposed01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
