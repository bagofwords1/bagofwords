"""add pptx_path to artifacts

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-02-14 10:00:00.000000

Adds pptx_path column to artifacts table for storing
the path to generated PPTX files for slides mode.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 's4t5u6v7w8x9'
down_revision: Union[str, None] = 'r3s4t5u6v7w8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('artifacts', sa.Column('pptx_path', sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column('artifacts', 'pptx_path')
