"""add thumbnail_path to artifacts

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-02-07 10:00:00.000000

Adds thumbnail_path column to artifacts table for storing
preview thumbnails on the home page.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'r3s4t5u6v7w8'
down_revision: Union[str, None] = 'q2r3s4t5u6v7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('artifacts', sa.Column('thumbnail_path', sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column('artifacts', 'thumbnail_path')
