from sqlalchemy import Column, ForeignKey, PrimaryKeyConstraint, String, UniqueConstraint

from app.models.base import BaseSchema


class ArtifactStar(BaseSchema):
    """One user's star on one dashboard (migration dashlist01).

    Personal: two readers of the same dashboard star it independently, and
    starred dashboards come first in that user's list. Unstarring deletes
    the row.
    """
    __tablename__ = "artifact_stars"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_artifact_stars"),
        UniqueConstraint("artifact_id", "user_id", name="uq_artifact_stars_artifact_user"),
    )

    artifact_id = Column(String(36), ForeignKey("artifacts.id", name="fk_artifact_stars_artifact_id",
                                                ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", name="fk_artifact_stars_user_id", ondelete="CASCADE"),
                     nullable=False, index=True)
