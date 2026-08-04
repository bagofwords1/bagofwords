"""Add build_contents.base_version_id — the baseline a pending change was written against

Revision ID: bcbase001
Revises: connfile01
Create Date: 2026-08-03

InstructionBuild.base_build_id records where a build FORKED, once, at creation.
Nothing rebases it, so every promotion of main while the build is open moves
main out from under it and the review diff (base_build -> proposed) starts
attributing main's own newer text to the suggestion.

base_version_id records the baseline per (build, instruction) instead: what the
instruction looked like when this build first changed it. NULL on existing rows,
where readers fall back to the old base-build lookup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bcbase001'
down_revision: Union[str, None] = 'connfile01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No FK constraint: SQLite cannot add one to an existing table without a
    # full table rebuild, and this column is a soft pointer read through a
    # fallback — a dangling value degrades to the old behavior, it never breaks
    # a read.
    with op.batch_alter_table('build_contents') as batch_op:
        batch_op.add_column(
            sa.Column('base_version_id', sa.String(length=36), nullable=True)
        )
    op.create_index(
        'ix_build_contents_base_version_id', 'build_contents', ['base_version_id']
    )


def downgrade() -> None:
    op.drop_index('ix_build_contents_base_version_id', table_name='build_contents')
    with op.batch_alter_table('build_contents') as batch_op:
        batch_op.drop_column('base_version_id')
