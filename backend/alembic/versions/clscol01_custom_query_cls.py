"""column-level security on custom queries

Revision ID: clscol01
Revises: shwinfo01
Create Date: 2026-11-12 00:00:00.000000

A custom query is materialized once with a shared credential, so the cached copy
holds every column the credential could read. These columns carry the policy
that projects that copy at read time: each identity gets back only the columns
it is allowed, exactly as `rls_*` filters the rows.

Unlike rows there is no `default_deny` flag: a column is restricted explicitly
(named in `cls_policy.restricted_columns`) and stays hidden unless a grant names
it, so there is no unresolved state whose default could leak the column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'clscol01'
down_revision: Union[str, None] = 'shwinfo01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'connection_tables',
        sa.Column('cls_enabled', sa.Boolean(), nullable=False, server_default='0'),
    )
    op.add_column('connection_tables', sa.Column('cls_policy', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('connection_tables', 'cls_policy')
    op.drop_column('connection_tables', 'cls_enabled')
