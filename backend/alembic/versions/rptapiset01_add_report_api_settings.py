"""add per-conversation API opt-outs to reports

Revision ID: rptapiset01
Revises: mrgheads09
Create Date: 2026-10-14 00:00:00.000000

follow_ups / suggest_instructions (booleans) and tools ({public tool key:
false}) let an API integration switch off end-of-turn steps and agent tools
for a whole conversation. All nullable: null = the org setting decides, so
existing reports are unaffected.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'rptapiset01'
down_revision: Union[str, None] = 'mrgheads09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reports', sa.Column('follow_ups', sa.Boolean(), nullable=True))
    op.add_column('reports', sa.Column('suggest_instructions', sa.Boolean(), nullable=True))
    op.add_column('reports', sa.Column('tools', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('reports') as batch_op:
        batch_op.drop_column('tools')
        batch_op.drop_column('suggest_instructions')
        batch_op.drop_column('follow_ups')
