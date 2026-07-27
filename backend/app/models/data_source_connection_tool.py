from sqlalchemy import Column, String, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import backref, relationship

from app.models.base import BaseSchema


class DataSourceConnectionTool(BaseSchema):
    """Per-agent (DataSource) overlay of a ConnectionTool.

    When present, overrides the org-wide ConnectionTool defaults
    (``is_enabled``, ``policy``) for a specific DataSource. The runtime
    tool loader reads the overlay first and falls back to the
    ConnectionTool row.
    """

    __tablename__ = "data_source_connection_tool"
    __table_args__ = (
        UniqueConstraint(
            "data_source_id", "connection_tool_id", name="uq_dsct_ds_tool"
        ),
    )

    data_source_id = Column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_tool_id = Column(
        String(36), ForeignKey("connection_tools.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_enabled = Column(Boolean, nullable=False, default=True)
    policy = Column(String, nullable=False, default="allow")  # allow | confirm | deny

    # ORM-side cascade: without it, deleting an agent (or a connection tool)
    # makes the ORM NULL the FK on its overlay rows — a NOT NULL violation —
    # because the DDL-level ON DELETE CASCADE is not enforced on SQLite (no
    # foreign_keys pragma). Overlay rows are few (≤ tool count), so loading
    # and deleting them explicitly is cheap and portable.
    data_source = relationship(
        "DataSource",
        backref=backref("tool_overlays", cascade="all, delete-orphan"),
    )
    connection_tool = relationship(
        "ConnectionTool",
        backref=backref("data_source_overlays", cascade="all, delete-orphan"),
    )
