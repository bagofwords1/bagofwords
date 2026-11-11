"""merge scheduled-task agents, slack channel routing, member role override and context budget heads

Revision ID: rel0614
Revises: schedagents01, extplatchanlist01, roleovr01, ctxcomp02
Create Date: 2026-11-10 18:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'rel0614'
down_revision: Union[str, None] = ('schedagents01', 'extplatchanlist01', 'roleovr01', 'ctxcomp02')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
