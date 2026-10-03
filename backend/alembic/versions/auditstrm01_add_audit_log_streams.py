"""audit log streams

Revision ID: auditstrm01
Revises: saml1001

Adds audit_log_streams (per-org SIEM/bucket delivery targets with a resumable
cursor) and an (organization_id, created_at, id) index on audit_logs so the
exporter's cursor scan and the export endpoint stay index-only.
"""
from alembic import op
import sqlalchemy as sa

revision = "auditstrm01"
down_revision = "saml1001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_log_streams",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("deleted_at", sa.DateTime()),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("destination", sa.String(32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secrets", sa.Text()),
        sa.Column("action_filter", sa.JSON()),
        sa.Column("state", sa.String(16), nullable=False, server_default="inactive"),
        sa.Column("start_from", sa.String(16), nullable=False, server_default="now"),
        sa.Column("cursor_created_at", sa.DateTime()),
        sa.Column("cursor_id", sa.String(36)),
        sa.Column("delivered_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_delivered_at", sa.DateTime()),
        sa.Column("last_attempt_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )
    op.create_index("ix_audit_log_streams_id", "audit_log_streams", ["id"], unique=True)
    op.create_index("ix_audit_log_streams_organization_id", "audit_log_streams", ["organization_id"])
    op.create_index("ix_audit_logs_org_created_id", "audit_logs", ["organization_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_audit_logs_org_created_id", table_name="audit_logs")
    op.drop_index("ix_audit_log_streams_organization_id", table_name="audit_log_streams")
    op.drop_index("ix_audit_log_streams_id", table_name="audit_log_streams")
    op.drop_table("audit_log_streams")
