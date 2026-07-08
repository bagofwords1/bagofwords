"""add evidence to instruction_versions

Brief provenance note for AI-suggested instruction versions (captured from the
create/edit_instruction tool calls in the knowledge harness and training mode).
Surfaced per hunk in the Knowledge Explorer review.

Revision ID: e7f8a9b0c1d2
Revises: trig0002
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, None] = 'trig0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instruction_versions', sa.Column('evidence', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('instruction_versions', 'evidence')
