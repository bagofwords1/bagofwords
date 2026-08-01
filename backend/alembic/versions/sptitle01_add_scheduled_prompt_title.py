"""add title to scheduled_prompts

Revision ID: sptitle01
Revises: 297905a87c8a
Create Date: 2026-08-01 00:00:00.000000

Gives scheduled tasks their own display name instead of borrowing the host
report's title. Nullable: existing rows keep falling back to the report title
(or a prompt snippet) in the UI.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'sptitle01'
down_revision: Union[str, None] = '297905a87c8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('scheduled_prompts', sa.Column('title', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('scheduled_prompts', 'title')
