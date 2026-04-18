"""add tags_json to test_cases

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-04-18 00:00:00.000000

Free-form tags per test case so the pytest harness (and future UI) can
filter cases by group — "artifacts", "data", "clarify", etc. Stored as
nullable JSON (list of strings) for SQLite/Postgres portability.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'test_cases',
        sa.Column('tags_json', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('test_cases', 'tags_json')
