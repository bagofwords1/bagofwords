"""Persist background delivery through dispatch and worker loss."""

from alembic import op
import sqlalchemy as sa

revision = "bgjob01"
down_revision = "grpllm01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("deleted_at", sa.DateTime()),
        sa.Column("key", sa.String(512), nullable=False, unique=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("owner", sa.String(36)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(36)),
        sa.Column("completion_id", sa.String(36)),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_background_jobs_due", "background_jobs", ["status", "available_at"])


def downgrade():
    op.drop_table("background_jobs")
