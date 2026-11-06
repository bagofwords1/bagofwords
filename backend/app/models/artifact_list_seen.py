from sqlalchemy import Column, DateTime, ForeignKey, PrimaryKeyConstraint, String, UniqueConstraint

from app.models.base import BaseSchema


class ArtifactListSeen(BaseSchema):
    """When a user last looked at the Shared dashboards of an organization
    (migration dashlist01): the watermark a shared dashboard is "New" against.
    One row per (user, organization)."""
    __tablename__ = "artifact_list_seen"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_artifact_list_seen"),
        UniqueConstraint("user_id", "organization_id", name="uq_artifact_list_seen_user_org"),
    )

    user_id = Column(String(36), ForeignKey("users.id", name="fk_artifact_list_seen_user_id", ondelete="CASCADE"),
                     nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id", name="fk_artifact_list_seen_organization_id",
                                                    ondelete="CASCADE"), nullable=False)
    shared_seen_at = Column(DateTime, nullable=False)
