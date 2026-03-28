from sqlalchemy import Column, String, Boolean, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class UserConnectionTool(BaseSchema):
    """
    Tracks per-user tool visibility for connections with auth_policy = "user_required".
    Records which tools a specific user can invoke.
    Parallel to UserConnectionTable for database connections.
    """
    __tablename__ = "user_connection_tools"
    __table_args__ = (
        UniqueConstraint('connection_id', 'user_id', 'tool_name', name='uq_user_conn_tool'),
    )

    connection_id = Column(String(36), ForeignKey('connections.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    tool_name = Column(String, nullable=False)
    connection_tool_id = Column(String(36), ForeignKey('connection_tools.id'), nullable=True)

    # Visibility and status for this user
    is_accessible = Column(Boolean, nullable=False, default=True)
    status = Column(String, nullable=False, default="accessible")  # accessible | inaccessible | unknown

    # Provenance and diagnostics
    metadata_json = Column(JSON, nullable=True)

    # Relationships
    connection = relationship("Connection", back_populates="user_tools", lazy="selectin")
    user = relationship("User", lazy="selectin")
    connection_tool = relationship("ConnectionTool", back_populates="user_overlays", lazy="selectin")
