from sqlalchemy import JSON, Column, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import backref, relationship

from app.models.base import BaseSchema


class DataSourceConnectionSetting(BaseSchema):
    """Per-agent (DataSource) settings for one of its connections.

    A connection is shared by every agent linked to it; this row narrows what
    ONE agent reads through it, without a second connection (and a second
    sign-in per user). Today it carries the Slack channel scope:

        {"allowed_channels": [{"id": "C0123ABCD", "name": "support", "is_private": false}, ...],
         "include_direct_messages": true}

    Channels are kept by Slack's stable id (a rename never breaks the scope);
    the name is only for display, and a private channel's name is never shown
    to viewers (see ``slack_channel_scope.redact_private_names``). No row, or an empty ``allowed_channels``,
    means every channel the signed-in user belongs to. The scope only ever
    narrows: the user's own Slack membership still decides what is visible.
    """

    __tablename__ = "data_source_connection_settings"
    __table_args__ = (
        UniqueConstraint("data_source_id", "connection_id", name="uq_dscs_ds_conn"),
    )

    data_source_id = Column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id = Column(
        String(36), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    settings = Column(JSON, nullable=False, default=dict)

    # ORM-side cascade (as DataSourceConnectionTool): SQLite doesn't enforce
    # the DDL ON DELETE CASCADE, so deleting an agent must delete these rows.
    data_source = relationship(
        "DataSource",
        backref=backref("connection_settings", cascade="all, delete-orphan"),
    )
