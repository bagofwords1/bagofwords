"""audit log streams

Revision ID: auditstrm01
Revises: saml1001

Adds audit_log_streams (per-org SIEM/bucket delivery targets with a resumable
cursor), audit_logs.export_seq (a visibility-ordered sequence the exporter
stamps on rows once they are committed, so a stream cursor can never step over
a row that commits late), the single-row audit_export_state (stamper lease +
last sequence), and an (organization_id, created_at, id) index for the
export endpoint's keyset scan.
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
        sa.Column("cursor_seq", sa.BigInteger()),
        sa.Column("start_after", sa.DateTime()),
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
    op.add_column("audit_logs", sa.Column("export_seq", sa.BigInteger(), nullable=True))
    op.create_index("ix_audit_logs_org_export_seq", "audit_logs", ["organization_id", "export_seq"])
    op.create_table(
        "audit_export_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stamp_lease_until", sa.DateTime()),
    )


def downgrade():
    op.drop_table("audit_export_state")
    op.drop_index("ix_audit_logs_org_export_seq", table_name="audit_logs")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_column("export_seq")
    op.drop_index("ix_audit_logs_org_created_id", table_name="audit_logs")
    op.drop_index("ix_audit_log_streams_organization_id", table_name="audit_log_streams")
    op.drop_index("ix_audit_log_streams_id", table_name="audit_log_streams")
    op.drop_table("audit_log_streams")
