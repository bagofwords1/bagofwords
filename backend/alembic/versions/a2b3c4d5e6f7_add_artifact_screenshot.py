"""add screenshot_base64 and render_errors to artifacts

Revision ID: a2b3c4d5e6f7
Revises: z1a2b3c4d5e6
Create Date: 2026-04-02 00:00:00.000000

Stores the last preview screenshot (base64 PNG) and JS render errors on
artifacts so read_artifact can return them without re-rendering.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'z1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('artifacts', sa.Column('screenshot_base64', sa.Text(), nullable=True))
    op.add_column('artifacts', sa.Column('render_errors', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('artifacts', 'render_errors')
    op.drop_column('artifacts', 'screenshot_base64')
