"""Artifact-owned resource definitions, encrypted records, and minimal views."""

from alembic import op
import sqlalchemy as sa

revision = "artres01"
down_revision = "umbr0928"
branch_labels = None
depends_on = None


def base():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("deleted_at", sa.DateTime()),
    ]


def upgrade():
    op.create_table(
        "artifact_storage_budgets",
        *base(),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("organization_id", "actor_id", name="uq_artifact_storage_budget"),
    )
    op.create_table(
        "artifact_publications",
        *base(),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False, unique=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("artifact_versions.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "artifact_resources",
        *base(),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("artifact_id", "name", name="uq_artifact_resource_name"),
    )
    op.create_index("ix_artifact_resources_artifact_id", "artifact_resources", ["artifact_id"])
    op.create_table(
        "artifact_records",
        *base(),
        sa.Column("resource_id", sa.String(36), sa.ForeignKey("artifact_resources.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
    )
    op.create_index("ix_artifact_records_page", "artifact_records", ["resource_id", "id"])
    op.create_index("ix_artifact_records_created", "artifact_records", ["resource_id", "created_at", "id"])
    op.create_index("ix_artifact_records_owner", "artifact_records", ["resource_id", "owner_id", "id"])
    op.create_table(
        "artifact_record_indexes",
        *base(),
        sa.Column("resource_id", sa.String(36), sa.ForeignKey("artifact_resources.id"), nullable=False),
        sa.Column("record_id", sa.String(36), sa.ForeignKey("artifact_records.id"), nullable=False),
        sa.Column("field", sa.String(63), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("unique_hash", sa.String(64)),
        sa.UniqueConstraint("resource_id", "field", "unique_hash", name="uq_artifact_record_value"),
    )
    op.create_index("ix_artifact_index_lookup", "artifact_record_indexes", ["resource_id", "field", "value_hash"])
    op.create_index("ix_artifact_record_indexes_record_id", "artifact_record_indexes", ["record_id"])
    op.create_table(
        "artifact_mutations",
        *base(),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.UniqueConstraint("artifact_id", "actor_id", "key", name="uq_artifact_mutation"),
    )
    op.create_table(
        "artifact_views",
        *base(),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("viewer_hash", sa.String(64)),
        sa.Column("surface", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_artifact_views_day", "artifact_views", ["artifact_id", "created_at"])

    op.create_table(
        "artifact_view_days",
        *base(),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("surface", sa.String(16), nullable=False),
        sa.Column("viewer_hash", sa.String(64), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.UniqueConstraint("artifact_id", "day", "surface", "viewer_hash", name="uq_artifact_view_day"),
    )
    op.create_index("ix_artifact_view_days_range", "artifact_view_days", ["artifact_id", "day"])

    op.create_table(
        "artifact_file_bindings",
        *base(),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("resource_id", sa.String(36), sa.ForeignKey("artifact_resources.id"), nullable=False),
        sa.Column("file_id", sa.String(36), sa.ForeignKey("files.id"), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
    )
    op.create_index("ix_artifact_file_bindings_artifact_id", "artifact_file_bindings", ["artifact_id"])
    op.create_index("ix_artifact_file_bindings_resource_id", "artifact_file_bindings", ["resource_id"])

    op.create_table(
        "artifact_rate_buckets",
        *base(),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("bucket", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("scope_hash", "bucket", name="uq_artifact_rate_bucket"),
    )
    op.create_index("ix_artifact_rate_expiry", "artifact_rate_buckets", ["bucket"])
    op.create_index("ix_artifact_mutations_retention", "artifact_mutations", ["created_at"])
    op.create_index("ix_artifact_views_retention", "artifact_views", ["created_at"])
    op.create_index("ix_artifact_view_days_retention", "artifact_view_days", ["day"])


def downgrade():
    for name in (
        "artifact_storage_budgets",
        "artifact_view_days",
        "artifact_publications",
        "artifact_rate_buckets",
        "artifact_file_bindings",
        "artifact_views",
        "artifact_mutations",
        "artifact_record_indexes",
        "artifact_records",
        "artifact_resources",
    ):
        op.drop_table(name)
