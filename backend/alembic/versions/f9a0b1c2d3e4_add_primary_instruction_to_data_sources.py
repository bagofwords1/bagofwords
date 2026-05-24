"""add primary_instruction_id to data_sources

Revision ID: f9a0b1c2d3e4
Revises: b4c5d6e7f8a9, e9f0a1b2c3d4
Create Date: 2026-05-23

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, Sequence[str], None] = ('b4c5d6e7f8a9', 'e9f0a1b2c3d4')
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('data_sources', sa.Column('primary_instruction_id', sa.String(36), nullable=True))


def downgrade():
    op.drop_column('data_sources', 'primary_instruction_id')
