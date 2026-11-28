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


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns('connection_tables')}


# Idempotent in both directions: the dashboard test corpus replays schema-only
# revisions unstamped (tests/fixtures/dashboard_corpus/alembic_at.py).
def upgrade() -> None:
    existing = _columns()
    if 'cls_enabled' not in existing:
        op.add_column(
            'connection_tables',
            sa.Column('cls_enabled', sa.Boolean(), nullable=False, server_default='0'),
        )
    if 'cls_policy' not in existing:
        op.add_column('connection_tables', sa.Column('cls_policy', sa.JSON(), nullable=True))


def downgrade() -> None:
    existing = _columns()
    if 'cls_policy' in existing:
        op.drop_column('connection_tables', 'cls_policy')
    if 'cls_enabled' in existing:
        op.drop_column('connection_tables', 'cls_enabled')
