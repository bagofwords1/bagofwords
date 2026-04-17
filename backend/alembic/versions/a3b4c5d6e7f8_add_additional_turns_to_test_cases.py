"""add additional_turns_json to test_cases

Revision ID: a3b4c5d6e7f8
Revises: e7f8g9h0i1j2
Create Date: 2026-04-17 00:00:00.000000

Backs multi-turn YAML-only eval cases. Turn 1 still lives in prompt_json so
the in-product UI keeps working unchanged; turns 2..N land in the new
additional_turns_json column. Nullable/JSON to stay compatible with both
SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'e7f8g9h0i1j2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'test_cases',
        sa.Column('additional_turns_json', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('test_cases', 'additional_turns_json')
