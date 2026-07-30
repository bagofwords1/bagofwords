"""row-level security on custom queries

Revision ID: fastq002
Revises: fastq001
Create Date: 2026-07-29 00:00:00.000000

A custom query is materialized once with a shared credential, so every agent
that activates it sees the same rows. These columns carry the policy that
filters that copy at read time against who is asking.

`rls_default_deny` defaults to true: a policy whose attribute cannot be
resolved for the requesting identity yields no rows. The opposite default
would hand over the whole table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fastq002'
down_revision: Union[str, None] = 'fastq001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'connection_tables',
        sa.Column('rls_enabled', sa.Boolean(), nullable=False, server_default='0'),
    )
    op.add_column('connection_tables', sa.Column('rls_mode', sa.String(), nullable=True))
    op.add_column('connection_tables', sa.Column('rls_policy', sa.JSON(), nullable=True))
    op.add_column(
        'connection_tables',
        sa.Column('rls_default_deny', sa.Boolean(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    op.drop_column('connection_tables', 'rls_default_deny')
    op.drop_column('connection_tables', 'rls_policy')
    op.drop_column('connection_tables', 'rls_mode')
    op.drop_column('connection_tables', 'rls_enabled')
