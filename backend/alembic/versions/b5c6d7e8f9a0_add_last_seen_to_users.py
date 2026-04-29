"""add last_seen to users

Revision ID: b5c6d7e8f9a0
Revises: f2a3b4c5d6e7
Create Date: 2026-04-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_seen', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_seen')
