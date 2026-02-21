"""add completion_id to report_file_association

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-02-21 10:00:00.000000

Adds completion_id column to report_file_association table to track
which completion used each file (especially images). This allows:
- Frontend to filter out files already used in completions
- Chat UI to display images attached to specific completions
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 't5u6v7w8x9y0'
down_revision: Union[str, None] = 's4t5u6v7w8x9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use batch mode for SQLite compatibility
    with op.batch_alter_table('report_file_association') as batch_op:
        batch_op.add_column(sa.Column('completion_id', sa.String(36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('report_file_association') as batch_op:
        batch_op.drop_column('completion_id')
