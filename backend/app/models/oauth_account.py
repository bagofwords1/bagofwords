from fastapi_users.db import SQLAlchemyBaseUserTable
from app.models.base import Base
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship, Mapped, mapped_column
from fastapi_users.db import SQLAlchemyBaseOAuthAccountTable
import uuid

class OAuthAccount(SQLAlchemyBaseOAuthAccountTable[str], Base):
    __tablename__ = "oauth_accounts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="cascade"), nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="oauth_accounts")