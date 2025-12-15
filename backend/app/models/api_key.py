from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ApiKey(BaseSchema):
    __tablename__ = "api_keys"

    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)  # e.g., "MCP Integration", "CI Pipeline"
    key_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256 hash
    key_prefix = Column(String(12), nullable=False)  # First 12 chars for identification (e.g., "bow_abc123...")
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="api_keys")
