"""merge connection alert and dashboard list heads

Revision ID: rel0611
Revises: connalertstate01, dashlist02
Create Date: 2026-11-06 13:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'rel0611'
down_revision: Union[str, None] = ('connalertstate01', 'dashlist02')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
