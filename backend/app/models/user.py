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
    completion_feedbacks = relationship("CompletionFeedback", back_populates="user", foreign_keys="CompletionFeedback.user_id", cascade="all, delete-orphan", lazy="select")
    reviewed_completion_feedbacks = relationship("CompletionFeedback", back_populates="reviewed_by_user", foreign_keys="CompletionFeedback.reviewed_by", cascade="all, delete-orphan", lazy="select")
    memberships = relationship("Membership", back_populates="user")
    organizations = relationship("Organization", secondary="memberships", back_populates="users")
    files = relationship("File", back_populates="user")
    #prompts = relationship("Prompt", back_populates="user", lazy="selectin")
    memories = relationship("Memory", back_populates="user", lazy="selectin")
    oauth_accounts: Mapped[list[OAuthAccount]] = relationship("OAuthAccount", back_populates="user", cascade="all, delete")
    git_repositories = relationship("GitRepository", back_populates="user")
    queries = relationship("Query", back_populates="user")
    
    external_user_mappings = relationship("ExternalUserMapping", back_populates="user", cascade="all, delete-orphan", lazy="selectin")
    user_data_source_credentials = relationship("UserDataSourceCredentials", back_populates="user", cascade="all, delete-orphan", lazy="selectin")
    api_keys = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan", lazy="selectin")



# from app.models.organization import Organization
# from app.models.membership import Membership
# from app.models.memory import Memory

# Ensure SQLAlchemy registers the dependent mapper before configuration
# by importing the model module after User is defined.
from app.models.user_data_source_credentials import UserDataSourceCredentials  # noqa: E402,F401