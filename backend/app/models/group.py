from sqlalchemy import Column, String, Text, ForeignKey, UniqueConstraint, Integer
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class Group(BaseSchema):
    __tablename__ = 'groups'
    __table_args__ = (
        UniqueConstraint('organization_id', 'name', name='uq_groups_org_name'),
    )

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    external_id = Column(String, nullable=True)       # AD/Okta/SCIM group ID
    external_provider = Column(String, nullable=True)  # "azure_ad", "okta", "scim"
    # Group-level LLM defaults (EE llm_access_control). Soft references to
    # llm_models.id — stale values are skipped at resolution time. A group's
    # default also grants its members `use` of that model, even when restricted.
    default_llm_model_id = Column(String(36), nullable=True)
    small_default_llm_model_id = Column(String(36), nullable=True)
    # Which group's default wins for a user in several groups: lower wins,
    # ties broken by group name.
    default_llm_priority = Column(Integer, nullable=False, default=100, server_default="100")

    organization = relationship("Organization", back_populates="groups")
    memberships = relationship("GroupMembership", back_populates="group", cascade="all, delete-orphan")
