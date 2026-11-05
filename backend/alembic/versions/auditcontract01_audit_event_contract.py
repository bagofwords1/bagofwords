"""audit event contract v2: event time, outcome, correlation, actor snapshot; platform stream

Revision ID: auditcontract01
Revises: remotepolicy01
Create Date: 2026-11-05 00:00:00.000000

Additive only (docs/design/audit-event-contract.md). Existing rows keep NULL
in every new column and are exported as legacy records; nothing is backfilled
because the original event time, outcome and identity cannot be known after
the fact and must not be invented.

audit_log_streams.organization_id becomes nullable for the single platform
stream that receives events without an organization (configured in
bow-config, never through the org-scoped API).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'auditcontract01'
down_revision: Union[str, None] = 'remotepolicy01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


NEW_COLUMNS = (
    ("occurred_at", sa.DateTime()),
    ("outcome", sa.String(16)),
    ("failure_reason", sa.String(255)),
    ("request_id", sa.String(128)),
    ("session_id", sa.String(64)),
    ("actor_type", sa.String(16)),
    ("actor_snapshot", sa.JSON()),
    ("schema_version", sa.Integer()),
)


def upgrade() -> None:
    # Idempotent: the dashboard corpus fixture replays schema-only revisions
    # unstamped, then `upgrade head` must find the work done.
    have = _columns('audit_logs')
    missing = [(n, t) for n, t in NEW_COLUMNS if n not in have]
    idx = _indexes('audit_logs')
    if missing or 'ix_audit_logs_request' not in idx or 'ix_audit_logs_org_occurred' not in idx:
        with op.batch_alter_table('audit_logs', schema=None) as batch_op:
            for name, type_ in missing:
                batch_op.add_column(sa.Column(name, type_, nullable=True))
            if 'ix_audit_logs_request' not in idx:
                batch_op.create_index('ix_audit_logs_request', ['request_id'], unique=False)
            if 'ix_audit_logs_org_occurred' not in idx:
                batch_op.create_index('ix_audit_logs_org_occurred', ['organization_id', 'occurred_at'], unique=False)

    stream_cols = _columns('audit_log_streams')
    with op.batch_alter_table('audit_log_streams', schema=None) as batch_op:
        batch_op.alter_column('organization_id', existing_type=sa.String(36), nullable=True)
        if 'degraded_at' not in stream_cols:
            batch_op.add_column(sa.Column('degraded_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log_streams WHERE organization_id IS NULL")
    stream_cols = _columns('audit_log_streams')
    with op.batch_alter_table('audit_log_streams', schema=None) as batch_op:
        if 'degraded_at' in stream_cols:
            batch_op.drop_column('degraded_at')
        batch_op.alter_column('organization_id', existing_type=sa.String(36), nullable=False)

    have = _columns('audit_logs')
    idx = _indexes('audit_logs')
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        for name in ('ix_audit_logs_org_occurred', 'ix_audit_logs_request'):
            if name in idx:
                batch_op.drop_index(name)
        for name, _ in reversed(NEW_COLUMNS):
            if name in have:
                batch_op.drop_column(name)
