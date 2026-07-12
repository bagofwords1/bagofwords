from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class UserConnectionToolPreference(BaseSchema):
    """Per-user policy preference for a single connection tool.

    Sits on top of the admin layers (``ConnectionTool.policy`` default and the
    per-agent ``DataSourceConnectionTool`` overlay). Resolution order at run
    time: user preference > agent overlay > connection default — except that an
    admin ``deny`` is absolute and cannot be relaxed by a user preference.

    Rows are keyed by ``connection_tool_id`` (stable across tool re-discovery,
    which upserts by name) so preferences follow the tool and are cascade-removed
    when the tool disappears from the server.
    """

    __tablename__ = "user_connection_tool_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "connection_tool_id", name="uq_uctp_user_tool"),
    )

    user_id = Column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_tool_id = Column(
        String(36), ForeignKey("connection_tools.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy = Column(String, nullable=False)  # allow | ask | deny | auto

    user = relationship("User", lazy="selectin")
    connection_tool = relationship("ConnectionTool", backref="user_preferences")
