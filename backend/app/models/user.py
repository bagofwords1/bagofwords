from sqlalchemy import Column, String, DateTime
from typing import List
from sqlalchemy.orm import relationship
from fastapi_users.db import SQLAlchemyBaseUserTable
from app.models.base import BaseSchema
from app.models.base import Base
from app.models.oauth_account import OAuthAccount
import uuid
from sqlalchemy.orm import Mapped, mapped_column

class User(SQLAlchemyBaseUserTable[str], Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    name = Column(String, index=True, nullable=False)
    last_login = Column(DateTime, nullable=True)

    reports = relationship("Report", back_populates="user")
    completions = relationship("Completion", back_populates="user")
    memberships = relationship("Membership", back_populates="user")
    organizations = relationship("Organization", secondary="memberships", back_populates="users")
    files = relationship("File", back_populates="user")
    #prompts = relationship("Prompt", back_populates="user", lazy="selectin")
    memories = relationship("Memory", back_populates="user", lazy="selectin")
    oauth_accounts: Mapped[list[OAuthAccount]] = relationship("OAuthAccount", back_populates="user", cascade="all, delete")
    git_repositories = relationship("GitRepository", back_populates="user")
    
    external_user_mappings = relationship("ExternalUserMapping", back_populates="user", cascade="all, delete-orphan")



# from app.models.organization import Organization
# from app.models.membership import Membership
# from app.models.memory import Memory