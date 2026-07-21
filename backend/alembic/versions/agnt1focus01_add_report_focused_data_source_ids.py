"""add focused_data_source_ids to reports

Revision ID: agnt1focus01
Revises: mrgheads02
Create Date: 2026-07-18 00:00:00.000000

Agent focus: a JSON list on reports naming the subset of attached agents
(data sources) whose FULL schema is rendered into the planner context. When
an org has many agents, the planner shows a thin roster of all attached agents
and full schema only for the focused subset (auto-seeded from per-user usage or
narrowed via the set_report_agents tool / prompt-box focus selector).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'agnt1focus01'
down_revision: Union[str, None] = 'mrgheads02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reports', sa.Column('focused_data_source_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('reports', 'focused_data_source_ids')
