"""add publish_status to data_sources

Revision ID: d6d9a78b7b4a
Revises: sf4a5b6c7d8e
Create Date: 2026-06-14 00:00:00.000000

Adds the manager-set publishing lifecycle column to data sources.

``publish_status`` is distinct from ``is_active`` (connection health,
system-managed). Values:
  - published — visible to everyone with access (default)
  - draft     — visible only to users who can ``manage`` the agent
  - disabled  — off; hidden everywhere

Stored as a plain string (not a DB enum) for clean SQLite/Postgres parity.
Existing rows are backfilled to ``published`` via the server default.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6d9a78b7b4a'
down_revision: Union[str, None] = 'sf4a5b6c7d8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.add_column(
            sa.Column(
                'publish_status',
                sa.String(),
                nullable=False,
                server_default='published',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('data_sources') as batch_op:
        batch_op.drop_column('publish_status')
