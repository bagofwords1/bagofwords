"""add memory_entries (per-user memory) + agent_executions.memory_context_json

Revision ID: usrmem01
Revises: artchatmodel01
Create Date: 2026-09-26 00:00:00.000000

Replaces the single ``memberships.memory`` document with one row per durable,
personal fact. Existing memory text is migrated line-by-line (idempotent; see
app/services/memory_migration.py). ``memberships.memory`` is kept, read-only,
for one release so the change can be rolled back.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'usrmem01'
down_revision: Union[str, None] = 'artchatmodel01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memory_entries',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('handle', sa.String(length=12), nullable=False),
        sa.Column('section', sa.String(length=16), nullable=False),
        sa.Column('text', sa.Text(), nullable=False, server_default=''),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('aliases', sa.JSON(), nullable=True),
        sa.Column('event_start', sa.DateTime(), nullable=True),
        sa.Column('event_end', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('source', sa.String(length=12), nullable=False, server_default='agent'),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('seen_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=12), nullable=False, server_default='active'),
        sa.Column('superseded_by_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'user_id', 'seq', name='uq_memory_entries_org_user_seq'),
    )
    op.create_index('ix_memory_entries_id', 'memory_entries', ['id'], unique=True)
    op.create_index('ix_memory_entries_organization_id', 'memory_entries', ['organization_id'])
    op.create_index('ix_memory_entries_user_id', 'memory_entries', ['user_id'])
    op.create_index('ix_memory_entries_status', 'memory_entries', ['status'])
    op.create_index(
        'ix_memory_entries_org_user_status', 'memory_entries', ['organization_id', 'user_id', 'status']
    )

    # Per-turn memory metadata for the trace (handles/sections/tiers/size and
    # refusals). Never read by anyone but the memory's owner in full.
    op.add_column('agent_executions', sa.Column('memory_context_json', sa.JSON(), nullable=True))

    from app.services.memory_migration import migrate_legacy_memory
    migrate_legacy_memory(op.get_bind())


def downgrade() -> None:
    op.drop_column('agent_executions', 'memory_context_json')
    op.drop_index('ix_memory_entries_org_user_status', table_name='memory_entries')
    op.drop_index('ix_memory_entries_status', table_name='memory_entries')
    op.drop_index('ix_memory_entries_user_id', table_name='memory_entries')
    op.drop_index('ix_memory_entries_organization_id', table_name='memory_entries')
    op.drop_index('ix_memory_entries_id', table_name='memory_entries')
    op.drop_table('memory_entries')
