"""add fixtures_json to test_cases

Revision ID: evalfix001
Revises: agnt1focus01
Create Date: 2026-07-31 00:00:00.000000

Eval case fixtures: declarative org state a case needs before the agent
runs (currently: pre-existing published instructions). Applied idempotently
at run start so cases like "resolve a conflicting instruction by editing
it" are self-contained and runnable from both the UI and the pytest
harness.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'evalfix001'
down_revision: Union[str, None] = 'agnt1focus01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('test_cases', sa.Column('fixtures_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('test_cases', 'fixtures_json')
