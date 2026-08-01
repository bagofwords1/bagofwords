"""bind standalone triggers to a project

Revision ID: whproj01
Revises: whlastev01
Create Date: 2026-08-01 12:00:00.000000

A trigger with a project spawns every session inside that project and appears
in the project's automations list. Nullable: existing triggers keep spawning
at the root.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'whproj01'
down_revision: Union[str, None] = 'whlastev01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('webhooks', sa.Column('project_id', sa.String(length=36), nullable=True))
    op.create_index('ix_webhooks_project_id', 'webhooks', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_webhooks_project_id', table_name='webhooks')
    op.drop_column('webhooks', 'project_id')
