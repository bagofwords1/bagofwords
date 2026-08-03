"""add catalog_key and catalog_version to instructions

Revision ID: sk1c2t3a4l5g
Revises: mrgheads04
Create Date: 2026-08-01 00:00:00.000000

Groundwork for pre-built skills, which will ship as files in the repo and be
installed into an organization as instructions with kind='skill'. These two
columns record which catalog entry a row was installed from, so the catalog
list can show installed state without matching on title, and so a future
"update available" check can compare versions.

The columns land ahead of the catalog itself so shipping it later needs no
migration. Nothing writes them yet.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'sk1c2t3a4l5g'
down_revision: Union[str, None] = 'mrgheads04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instructions', sa.Column('catalog_key', sa.String(length=100), nullable=True))
    op.add_column('instructions', sa.Column('catalog_version', sa.String(length=20), nullable=True))
    op.create_index('ix_instructions_catalog_key', 'instructions', ['catalog_key'])


def downgrade() -> None:
    op.drop_index('ix_instructions_catalog_key', table_name='instructions')
    op.drop_column('instructions', 'catalog_version')
    op.drop_column('instructions', 'catalog_key')
