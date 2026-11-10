from sqlalchemy import Column, String, DateTime, ForeignKey, UniqueConstraint
from app.models.base import BaseSchema


class ExternalPlatformChannel(BaseSchema):
    """A channel seen on an external-platform instance (discover-on-traffic).

    Slack/Teams don't hand us a list of channels, and fetching one needs extra
    OAuth scopes (heavy on Teams). Instead, every time the bot receives a
    channel message we record the channel here from the payload it already
    carries — its id, and a best-effort type/name. The settings UI reads this so
    an admin can map a channel to specific agents without any new scopes.

    Rows are informational: they drive the picker, not routing. The scope itself
    lives in ``external_platform_agents`` keyed on ``external_channel_id``.
    ``name`` is a best-effort / admin-supplied label (the Slack event payload
    carries only the channel id, so it starts null and the UI falls back to the
    id). Deleting a row just hides the channel from the picker until it is seen
    again.
    """

    __tablename__ = "external_platform_channels"
    __table_args__ = (
        UniqueConstraint(
            "platform_id", "external_channel_id", name="uq_external_platform_channel"
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
    external_channel_id = Column(String(255), nullable=False, index=True)
    # Best-effort display label (from the payload if present, else admin-set).
    name = Column(String(255), nullable=True)
    channel_type = Column(String(64), nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return (
            f"<ExternalPlatformChannel platform={self.platform_id} "
            f"channel={self.external_channel_id!r} name={self.name!r}>"
        )
