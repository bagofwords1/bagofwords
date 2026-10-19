from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from app.models.base import BaseSchema, metadata


class ArtifactRelease(BaseSchema):
    """The published, frozen state of a dashboard (migration dashfc01).

    A release holds its own copy of the dashboard content, with ids remapped
    to the queries and visualizations copied for it (those rows carry
    `artifact_release_id`). Viewers read the release; the owner keeps editing
    the draft versions. Publish (phase 5) writes them: one release per
    dashboard is live (artifacts.published_release_id), at most one is
    'publishing' (dashfc04), and a failed one stays as the record of why.
    """
    __tablename__ = "artifact_releases"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_artifact_releases"),
        Index("uq_artifact_releases_one_publishing", "artifact_id", unique=True,
              sqlite_where=text("status = 'publishing'"), postgresql_where=text("status = 'publishing'")),
    )

    artifact_id = Column(String(36), ForeignKey("artifacts.id", name="fk_artifact_releases_artifact_id",
                                                ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey("organizations.id",
                                                    name="fk_artifact_releases_organization_id"),
                             nullable=False, index=True)
    source_version_id = Column(String(36), ForeignKey("artifact_versions.id",
                                                      name="fk_artifact_releases_source_version_id",
                                                      ondelete="SET NULL"), nullable=True)
    content = Column(JSON, nullable=False, default=dict)
    # publishing | ready | failed
    status = Column(String(20), nullable=False, default="publishing", server_default="publishing")
    error = Column(Text, nullable=True)
    published_by = Column(String(36), ForeignKey("users.id", name="fk_artifact_releases_published_by"),
                          nullable=True)
    published_at = Column(DateTime, nullable=True)
    bow_source_access = Column(JSON, nullable=True)
    theme_name = Column(String, nullable=True)
    theme_overrides = Column(JSON, nullable=True)
    thumbnail_path = Column(String(512), nullable=True)
    pptx_path = Column(String(512), nullable=True)


artifact_release_data_source_association = Table(
    "artifact_release_data_source_association",
    metadata,
    Column("artifact_release_id", String(36),
           ForeignKey("artifact_releases.id", name="fk_artifact_release_ds_release_id", ondelete="CASCADE"),
           primary_key=True),
    Column("data_source_id", String(36),
           ForeignKey("data_sources.id", name="fk_artifact_release_ds_data_source_id", ondelete="CASCADE"),
           primary_key=True),
    PrimaryKeyConstraint("artifact_release_id", "data_source_id", name="pk_artifact_release_ds"),
)

artifact_release_file_association = Table(
    "artifact_release_file_association",
    metadata,
    Column("artifact_release_id", String(36),
           ForeignKey("artifact_releases.id", name="fk_artifact_release_file_release_id", ondelete="CASCADE"),
           primary_key=True),
    Column("file_id", String(36),
           ForeignKey("files.id", name="fk_artifact_release_file_file_id", ondelete="CASCADE"),
           primary_key=True),
    PrimaryKeyConstraint("artifact_release_id", "file_id", name="pk_artifact_release_file"),
)


class ArtifactShare(BaseSchema):
    """A grant of one dashboard to one user or one group (migration dashfc01).

    Replaces report_shares rows of share_type 'artifact' once the dashboards
    project moves sharing onto the dashboard. Exactly one principal per row
    (enforced by the database, unlike report_shares).
    """
    __tablename__ = "artifact_shares"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_artifact_shares"),
        UniqueConstraint("artifact_id", "user_id", name="uq_artifact_share_user"),
        UniqueConstraint("artifact_id", "group_id", name="uq_artifact_share_group"),
        CheckConstraint(
            "(user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)",
            name="ck_artifact_shares_one_principal",
        ),
    )

    artifact_id = Column(String(36), ForeignKey("artifacts.id", name="fk_artifact_shares_artifact_id",
                                                ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", name="fk_artifact_shares_user_id", ondelete="CASCADE"),
                     nullable=True, index=True)
    group_id = Column(String(36), ForeignKey("groups.id", name="fk_artifact_shares_group_id", ondelete="CASCADE"),
                      nullable=True, index=True)


class ArtifactSlugHistory(BaseSchema):
    """A name a dashboard had (migration dashfc06): it keeps leading to the
    dashboard, renamed or with its name removed, and no other dashboard
    takes it."""
    __tablename__ = "artifact_slug_history"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_artifact_slug_history"),
        UniqueConstraint("slug", name="uq_artifact_slug_history_slug"),
    )

    slug = Column(String(255), nullable=False)
    artifact_id = Column(String(36), ForeignKey("artifacts.id", name="fk_artifact_slug_history_artifact_id",
                                                ondelete="CASCADE"), nullable=False, index=True)
