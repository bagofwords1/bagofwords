from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from app.models.base import BaseSchema


class ExternalPlatformAgent(BaseSchema):
    """Scopes a single external-platform instance to specific agents.

    One row per (platform instance, agent/data source) that the instance
    exposes. This is how several instances of one channel type each serve a
    different set of agents — a WhatsApp number for sales, another for finance.

    Semantics are deliberately opt-in: an instance with *no* rows exposes the
    full set it would have exposed before (every agent the messaging user can
    access that is available on the channel), so existing connections are
    unchanged. When rows exist, the exposed set is narrowed to them — always
    still intersected with the user's own access, never widened.
    """

    __tablename__ = "external_platform_agents"
    __table_args__ = (
        UniqueConstraint(
            "platform_id", "data_source_id", name="uq_external_platform_agent"
        ),
    )

    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    platform_id = Column(
        String(36),
        ForeignKey("external_platforms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The agent exposed. Soft FK to data_sources.id; a deleted agent simply
    # drops out of the scope at resolution time.
    data_source_id = Column(
        String(36),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    def __repr__(self):
        return (
            f"<ExternalPlatformAgent platform={self.platform_id} "
            f"agent={self.data_source_id}>"
        )
