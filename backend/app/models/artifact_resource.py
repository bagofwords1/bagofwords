"""Artifact resources are independent of generated UI version content."""

from sqlalchemy import Column, String, Integer, Text, JSON, ForeignKey, UniqueConstraint, Index
from app.models.base import BaseSchema


class ArtifactResource(BaseSchema):
    __tablename__ = "artifact_resources"
    artifact_id = Column(String(36), ForeignKey("artifacts.id"), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    name = Column(String(63), nullable=False)
    kind = Column(String(20), nullable=False)
    definition = Column(JSON, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    count = Column(Integer, nullable=False, default=0)
    bytes = Column(Integer, nullable=False, default=0)
    lock_version = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("artifact_id", "name", name="uq_artifact_resource_name"),)


class ArtifactRecord(BaseSchema):
    __tablename__ = "artifact_records"
    resource_id = Column(String(36), ForeignKey("artifact_resources.id"), nullable=False)
    owner_id = Column(String(36), nullable=False)
    payload = Column(Text, nullable=False)  # Always encrypted; no plaintext fallback.
    revision = Column(Integer, nullable=False, default=1)
    size = Column(Integer, nullable=False)
    __table_args__ = (
        Index("ix_artifact_records_page", "resource_id", "id"),
        Index("ix_artifact_records_owner", "resource_id", "owner_id", "id"),
        Index("ix_artifact_records_created", "resource_id", "created_at", "id"),
    )


class ArtifactRecordIndex(BaseSchema):
    __tablename__ = "artifact_record_indexes"
    resource_id = Column(String(36), ForeignKey("artifact_resources.id"), nullable=False)
    record_id = Column(String(36), ForeignKey("artifact_records.id"), nullable=False, index=True)
    field = Column(String(63), nullable=False)
    value_hash = Column(String(64), nullable=False)
    unique_hash = Column(String(64), nullable=True)
    __table_args__ = (
        Index("ix_artifact_index_lookup", "resource_id", "field", "value_hash"),
        UniqueConstraint("resource_id", "field", "unique_hash", name="uq_artifact_record_value"),
    )


class ArtifactMutation(BaseSchema):
    __tablename__ = "artifact_mutations"
    artifact_id = Column(String(36), ForeignKey("artifacts.id"), nullable=False)
    actor_id = Column(String(36), nullable=False)
    key = Column(String(100), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    response = Column(Text, nullable=False)  # Encrypted record/resource acknowledgement.
    __table_args__ = (
        UniqueConstraint("artifact_id", "actor_id", "key", name="uq_artifact_mutation"),
        Index("ix_artifact_mutations_retention", "created_at"),
    )


class ArtifactView(BaseSchema):
    __tablename__ = "artifact_views"
    artifact_id = Column(String(36), ForeignKey("artifacts.id"), nullable=False)
    viewer_hash = Column(String(64), nullable=True)
    surface = Column(String(16), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    __table_args__ = (
        Index("ix_artifact_views_day", "artifact_id", "created_at"),
        Index("ix_artifact_views_retention", "created_at"),
    )


class ArtifactFileBinding(BaseSchema):
    __tablename__ = "artifact_file_bindings"
    artifact_id = Column(String(36), ForeignKey("artifacts.id"), nullable=False, index=True)
    resource_id = Column(String(36), ForeignKey("artifact_resources.id"), nullable=False, index=True)
    file_id = Column(String(36), ForeignKey("files.id"), nullable=False, unique=True)
    owner_id = Column(String(36), nullable=False)
    size = Column(Integer, nullable=False)


class ArtifactRateBucket(BaseSchema):
    """Expiring admission counts, never execution state or results."""

    __tablename__ = "artifact_rate_buckets"
    scope_hash = Column(String(64), nullable=False)
    bucket = Column(Integer, nullable=False)
    count = Column(Integer, nullable=False)
    __table_args__ = (
        UniqueConstraint("scope_hash", "bucket", name="uq_artifact_rate_bucket"),
        Index("ix_artifact_rate_expiry", "bucket"),
    )


class ArtifactPublication(BaseSchema):
    # Retired: retained solely for existing database compatibility; pins are ignored.
    """Optional pointer. Absence preserves historical newest-version behavior."""

    __tablename__ = "artifact_publications"
    artifact_id = Column(String(36), ForeignKey("artifacts.id"), nullable=False, unique=True)
    version_id = Column(String(36), ForeignKey("artifact_versions.id"), nullable=False)
    revision = Column(Integer, nullable=False, default=1)


class ArtifactViewDay(BaseSchema):
    """Daily counters by pseudonymous viewer; anonymous traffic has no identity."""

    __tablename__ = "artifact_view_days"
    artifact_id = Column(String(36), ForeignKey("artifacts.id"), nullable=False)
    day = Column(String(10), nullable=False)
    surface = Column(String(16), nullable=False)
    viewer_hash = Column(String(64), nullable=False, default="")
    views = Column(Integer, nullable=False, default=0)
    __table_args__ = (
        UniqueConstraint("artifact_id", "day", "surface", "viewer_hash", name="uq_artifact_view_day"),
        Index("ix_artifact_view_days_range", "artifact_id", "day"),
        Index("ix_artifact_view_days_retention", "day"),
    )


class ArtifactStorageBudget(BaseSchema):
    """Aggregate payload bytes; empty actor identifies the organization's budget."""

    __tablename__ = "artifact_storage_budgets"
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    actor_id = Column(String(36), nullable=False)
    bytes = Column(Integer, nullable=False, default=0)
    count = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("organization_id", "actor_id", name="uq_artifact_storage_budget"),)
