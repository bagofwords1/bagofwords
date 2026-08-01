"""add catalog_key and catalog_version to instructions

Revision ID: sk1c2t3a4l5g
Revises: 297905a87c8a
Create Date: 2026-08-01 00:00:00.000000

Pre-built skills ship as files in the repo (app/skills_catalog/) and are
installed into an organization as instructions with kind='skill'. These two
columns record which catalog entry a row was installed from, so the catalog
list can show installed state without matching on title, and so a future
"update available" check can compare versions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'sk1c2t3a4l5g'
down_revision: Union[str, None] = '297905a87c8a'
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
