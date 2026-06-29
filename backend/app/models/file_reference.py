"""A durable pin to a connector file (A3).

The report owns a *reference* (connection + provider file id); the bytes are
materialized on demand, per-user, each run — never stored on the reference (so
one user's bytes are never served to another, and the copy is always fresh).
"""
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship

from .base import BaseSchema


class FileReference(BaseSchema):
    __tablename__ = "file_references"

    report_id = Column(String(36), ForeignKey("reports.id"), nullable=False, index=True)
    connection_id = Column(String(36), ForeignKey("connections.id"), nullable=False, index=True)
    # Provider-native id of the file (e.g. a Drive fileId or an MCP resource uri).
    external_file_id = Column(String, nullable=False)
    name = Column(String, nullable=True)
    mime = Column(String, nullable=True)

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    connection = relationship("Connection")
