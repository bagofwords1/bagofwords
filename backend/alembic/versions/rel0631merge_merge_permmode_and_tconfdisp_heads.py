"""merge permmode and tconfdisp migration heads

Release 0.0.631 bundles #1786 (permmode01 -> permmode02) and #1847
(connact01 -> tconfdisp01). Both branch from rel0629 and add independent
columns (tool_confirmations.scope / reports.permission_mode /
data_sources.max_permission_mode / completions.started_via vs
connection_tools lock/effect/source and tool_confirmations.display), so
this revision only joins the two heads.

Revision ID: rel0631merge
Revises: permmode02, tconfdisp01
Create Date: 2026-11-21 18:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'rel0631merge'
down_revision: Union[str, None] = ('permmode02', 'tconfdisp01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
