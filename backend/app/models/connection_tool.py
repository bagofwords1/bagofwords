from sqlalchemy import Column, String, Boolean, ForeignKey, JSON, Text, UniqueConstraint
from sqlalchemy.sql.expression import false as sa_false
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ConnectionTool(BaseSchema):
    """
    Represents a tool discovered from an MCP server or custom API connection.
    Stores the tool definition (name, description, input/output schemas).
    Parallel to ConnectionTable for database connections.
    """
    __tablename__ = "connection_tools"
    __table_args__ = (
        UniqueConstraint('connection_id', 'name', name='uq_connection_tool_name'),
    )

    name = Column(String, nullable=False)
    connection_id = Column(String(36), ForeignKey('connections.id'), nullable=False, index=True)
    description = Column(Text, nullable=True)

    # Tool schema information
    input_schema = Column(JSON, nullable=True)   # JSON Schema for tool parameters
    output_schema = Column(JSON, nullable=True)  # JSON Schema for tool output (if known)

    # Admin controls
    is_enabled = Column(Boolean, nullable=False, default=True)
    policy = Column(String, nullable=False, default="allow")  # allow | ask | auto | deny
    # Locked: agents and users may make this tool's policy stricter, never looser.
    policy_locked = Column(Boolean, nullable=False, default=False, server_default=sa_false())

    # What calling the tool does: read | modify | external | destructive.
    # NULL = unknown (legacy rows); the UI and defaults treat it by hints.
    effect = Column(String, nullable=True)
    # Where the tool comes from: mcp | custom_api | remote | native.
    source = Column(String, nullable=True)

    # Additional metadata (version, tags, examples, etc.)
    metadata_json = Column(JSON, nullable=True)

    # Relationships
    connection = relationship("Connection", back_populates="connection_tools")
    user_overlays = relationship(
        "UserConnectionTool",
        back_populates="connection_tool",
        cascade="all, delete-orphan",
    )
