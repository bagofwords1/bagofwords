"""row- and column-level security on report queries

Revision ID: qrls0001
Revises: usrdsdef01
Create Date: 2026-08-08 00:00:00.000000

A report creator authors a policy against one query; it is enforced when
somebody ELSE opens the report or its artifact. Same policy vocabulary as the
connection-level policy on `connection_tables` (fastq002), different owner and
a different enforcement point — here every `execute_query` result is filtered
in app/services/query_access_policy.py.

The columns live on `queries` rather than `steps` because step rows are
regenerated on every rerun while the query is the stable identity of the
tracked insight across versions.

`rls_default_deny` defaults to true for the same reason it does on
connection_tables: an identity carrying no value for the attribute a policy
binds to must resolve to zero rows, never to the whole result.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'qrls0001'
down_revision: Union[str, None] = 'usrdsdef01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'queries',
        sa.Column('rls_enabled', sa.Boolean(), nullable=False, server_default='0'),
    )
    op.add_column('queries', sa.Column('rls_mode', sa.String(), nullable=True))
    op.add_column('queries', sa.Column('rls_policy', sa.JSON(), nullable=True))
    op.add_column(
        'queries',
        sa.Column('rls_default_deny', sa.Boolean(), nullable=False, server_default='1'),
    )
    op.add_column(
        'queries',
        sa.Column('cls_enabled', sa.Boolean(), nullable=False, server_default='0'),
    )
    op.add_column('queries', sa.Column('cls_policy', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('queries', 'cls_policy')
    op.drop_column('queries', 'cls_enabled')
    op.drop_column('queries', 'rls_default_deny')
    op.drop_column('queries', 'rls_policy')
    op.drop_column('queries', 'rls_mode')
    op.drop_column('queries', 'rls_enabled')
