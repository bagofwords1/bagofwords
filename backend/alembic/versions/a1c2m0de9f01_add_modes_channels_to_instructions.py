"""add applicable_modes and applicable_channels to instructions

Revision ID: a1c2m0de9f01
Revises: bd5c1a7e9f02
Create Date: 2026-06-22 00:00:00.000000

Adds two optional scoping fields to instructions:

* ``applicable_modes``    — which agent run-modes the instruction applies to
                            (e.g. ['chat', 'deep', 'training']).
* ``applicable_channels`` — which delivery channels it is enabled in
                            (e.g. ['app', 'slack', 'teams', 'email', 'mcp']).

For both fields ``NULL`` or an empty list means "applies everywhere" (all
modes / all channels), so existing rows keep their current behaviour.

The fields are versioned: they're snapshotted onto ``instruction_versions``
too, so edits create a new version and participate in build promotion/diffing.

Stored as nullable JSON on both tables for clean SQLite/Postgres parity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c2m0de9f01'
down_revision: Union[str, None] = 'bd5c1a7e9f02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('instructions') as batch_op:
        batch_op.add_column(sa.Column('applicable_modes', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('applicable_channels', sa.JSON(), nullable=True))
    with op.batch_alter_table('instruction_versions') as batch_op:
        batch_op.add_column(sa.Column('applicable_modes', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('applicable_channels', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('instruction_versions') as batch_op:
        batch_op.drop_column('applicable_channels')
        batch_op.drop_column('applicable_modes')
    with op.batch_alter_table('instructions') as batch_op:
        batch_op.drop_column('applicable_channels')
        batch_op.drop_column('applicable_modes')
