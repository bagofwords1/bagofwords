"""add dashboard_access_requests (request-to-view a dashboard)

Revision ID: dashacc01
Revises: artpopup01
Create Date: 2026-11-12 00:00:00.000000

A member who opens a dashboard link they cannot see may ask its editors for
view access with one click; the editors approve or deny from the share
settings. One row per request. A partial unique index keeps at most one
PENDING request per (artifact, requester) -- denied/approved rows stay as
history and do not hold the slot. Purely additive.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'dashacc01'
down_revision: Union[str, None] = 'artpopup01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table('dashboard_access_requests')


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped
    # (SCHEMA_ONLY_AFTER_DATA_MIGRATION), then `upgrade head` runs it again.
    if _has_table():
        return
    op.create_table(
        'dashboard_access_requests',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('artifact_id', sa.String(length=36), nullable=False),
        sa.Column('requester_user_id', sa.String(length=36), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=10), nullable=False, server_default='pending'),
        sa.Column('decided_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                name='fk_dashboard_access_requests_organization_id'),
        sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'],
                                name='fk_dashboard_access_requests_artifact_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requester_user_id'], ['users.id'],
                                name='fk_dashboard_access_requests_requester_user_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['decided_by_user_id'], ['users.id'],
                                name='fk_dashboard_access_requests_decided_by_user_id'),
        sa.PrimaryKeyConstraint('id', name='pk_dashboard_access_requests'),
    )
    op.create_index('ix_dashboard_access_requests_id', 'dashboard_access_requests', ['id'])
    op.create_index('ix_dashboard_access_requests_organization_id', 'dashboard_access_requests',
                    ['organization_id'])
    op.create_index('ix_dashboard_access_requests_artifact_id', 'dashboard_access_requests', ['artifact_id'])
    op.create_index('ix_dashboard_access_requests_requester_user_id', 'dashboard_access_requests',
                    ['requester_user_id'])
    op.create_index('ix_dashboard_access_requests_artifact_status', 'dashboard_access_requests',
                    ['artifact_id', 'status'])
    # At most one PENDING request per (artifact, requester). A partial unique
    # index so denied/approved history rows do not block a fresh request.
    op.create_index(
        'uq_dashboard_access_request_pending', 'dashboard_access_requests',
        ['artifact_id', 'requester_user_id'], unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    if not _has_table():
        return
    op.drop_index('uq_dashboard_access_request_pending', table_name='dashboard_access_requests')
    op.drop_index('ix_dashboard_access_requests_artifact_status', table_name='dashboard_access_requests')
    op.drop_index('ix_dashboard_access_requests_requester_user_id', table_name='dashboard_access_requests')
    op.drop_index('ix_dashboard_access_requests_artifact_id', table_name='dashboard_access_requests')
    op.drop_index('ix_dashboard_access_requests_organization_id', table_name='dashboard_access_requests')
    op.drop_index('ix_dashboard_access_requests_id', table_name='dashboard_access_requests')
    op.drop_table('dashboard_access_requests')
