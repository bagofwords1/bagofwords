"""add notifications (per-user inbox delivery layer)

Introduces the ``notifications`` table — the per-recipient delivery layer the
review feed and share/notify paths were missing. ``ReviewItem`` stays the
org/agent-scoped team queue; a ``Notification`` belongs to a single user and
owns that user's read/dismiss state. Review items fan out into notifications,
and the share + in-report-tool paths write directly into it.

Also merges the two existing heads (followups01 and c3f1a9b2d4e7) so the tree
collapses back to a single head.

Revision ID: notif0001
Revises: followups01, c3f1a9b2d4e7
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa


revision = "notif0001"
down_revision = ("followups01", "c3f1a9b2d4e7")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False, server_default="info"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("link", sa.String(), nullable=True),
        sa.Column("subject_json", sa.JSON(), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("group_key", sa.String(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_id", "notifications", ["id"], unique=True)
    op.create_index("ix_notifications_organization_id", "notifications", ["organization_id"])
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_source", "notifications", ["source"])
    op.create_index("ix_notifications_source_id", "notifications", ["source_id"])
    op.create_index("ix_notifications_group_key", "notifications", ["group_key"])
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])
    op.create_index("ix_notifications_user_read", "notifications", ["user_id", "read_at"])
    op.create_index("ix_notifications_dedup", "notifications", ["user_id", "source", "group_key"])


def downgrade() -> None:
    op.drop_index("ix_notifications_dedup", table_name="notifications")
    op.drop_index("ix_notifications_user_read", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index("ix_notifications_group_key", table_name="notifications")
    op.drop_index("ix_notifications_source_id", table_name="notifications")
    op.drop_index("ix_notifications_source", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_index("ix_notifications_organization_id", table_name="notifications")
    op.drop_index("ix_notifications_id", table_name="notifications")
    op.drop_table("notifications")
