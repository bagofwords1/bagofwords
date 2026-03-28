from sqlalchemy import Column, String, Boolean, ForeignKey, JSON, Text, UniqueConstraint
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
    policy = Column(String, nullable=False, default="allow")  # allow | confirm | deny

    # Additional metadata (version, tags, examples, etc.)
    metadata_json = Column(JSON, nullable=True)

    # Relationships
    connection = relationship("Connection", back_populates="connection_tools")
    user_overlays = relationship(
        "UserConnectionTool",
        back_populates="connection_tool",
        cascade="all, delete-orphan",
    )
