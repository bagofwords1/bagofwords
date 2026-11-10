from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from app.models.base import BaseSchema


class ExternalPlatformAgent(BaseSchema):
    """Scopes an external-platform instance — or one of its channels — to agents.

    One row per (platform instance, channel, agent/data source) that is exposed.
    This is how several instances of one channel type each serve a different set
    of agents — a WhatsApp number for sales, another for finance — and, for
    Slack/Teams where a workspace/tenant is a single instance, how an individual
    *channel* is pointed at specific agents.

    ``external_channel_id`` carries that second dimension:

    * ``""`` (empty) — the **connection default**: applies to the whole instance
      when no more specific channel row matches. This is the pre-existing
      per-instance meaning (every WhatsApp scope row uses it), so WhatsApp is
      unchanged.
    * a real channel id (Slack ``event.channel`` / Teams ``channelData.channel.id``)
      — scopes just that channel.

    Semantics stay deliberately opt-in: an instance/channel with *no* rows
    exposes the full set it would have exposed before (every agent the messaging
    user can access that is available on the channel). When rows exist, the
    exposed set is narrowed to them — always still intersected with the user's
    own access, never widened. Resolution prefers the channel row, then the
    ``""`` connection default, then "unscoped".
    """

    __tablename__ = "external_platform_agents"
    __table_args__ = (
        UniqueConstraint(
            "platform_id",
            "external_channel_id",
            "data_source_id",
            name="uq_external_platform_agent",
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
    # The channel this scope row applies to. "" (empty, the default) means the
    # whole instance / connection default; a non-empty value scopes one channel.
    # A sentinel "" rather than NULL keeps the unique constraint portable
    # (SQLite and Postgres both treat NULLs as distinct, which would allow
    # duplicate default rows).
    external_channel_id = Column(
        String(255), nullable=False, server_default="", default="", index=True
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
            f"channel={self.external_channel_id!r} agent={self.data_source_id}>"
        )
