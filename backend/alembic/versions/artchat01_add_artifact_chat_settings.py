"""add artifact chat settings to reports

Revision ID: artchat01
Revises: durctx01
Create Date: 2026-08-28 00:00:00.000000

Chat on the shared artifact page /r/{id}: owner-controlled toggle plus the
agent (data source) allowlist viewer chat may query. Viewer threads live on
hidden child reports (report_type='artifact_chat'), which needs no schema
change — report_type already exists.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'artchat01'
down_revision: Union[str, None] = 'durctx01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reports', sa.Column('artifact_chat_enabled', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('reports', sa.Column('artifact_chat_data_source_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('reports', 'artifact_chat_data_source_ids')
    op.drop_column('reports', 'artifact_chat_enabled')
