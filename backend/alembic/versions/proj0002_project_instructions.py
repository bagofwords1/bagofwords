"""add project instructions column

Revision ID: proj0002
Revises: proj0001
Create Date: 2026-07-26 00:00:00.000000

Project-local instructions: free text injected into the agent context for
every report in the project. Deliberately NOT rows in the instructions
table — no versioning/build machinery, just a field on the project.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'proj0002'
down_revision: Union[str, None] = 'proj0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('instructions', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'instructions')
