from sqlalchemy import Column, String
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.report import Report
from app.models.git_repository import GitRepository
from app.models.prompt import Prompt
from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping

class Organization(BaseSchema):
    __tablename__ = "organizations"
    
    name = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    
    memberships = relationship("Membership", back_populates="organization")
    reports = relationship("Report", back_populates="organization")
    users = relationship("User", secondary="memberships", back_populates="organizations", overlaps="memberships")
    files = relationship("File", back_populates="organization")
    data_sources = relationship("DataSource", back_populates="organization")
    memories = relationship("Memory", back_populates="organization")
    llm_providers = relationship("LLMProvider", back_populates="organization")
    llm_models = relationship("LLMModel", back_populates="organization")
    git_repositories = relationship("GitRepository", back_populates="organization")
    prompts = relationship("Prompt", back_populates="organization")
    settings = relationship("OrganizationSettings", uselist=False, back_populates="organization", cascade="all, delete-orphan")
    
    # External platform relationships
    external_platforms = relationship("ExternalPlatform", back_populates="organization", cascade="all, delete-orphan")
    external_user_mappings = relationship("ExternalUserMapping", back_populates="organization", cascade="all, delete-orphan")
    
    async def get_default_llm_model(self, db):
        """Get the default LLM model for the organization.
        
        Args:
            db: AsyncSession instance
        
        Returns:
            LLMModel: The enabled model marked as default, or the first enabled model if no default,
                     or None if no enabled models exist
        """
        # Load organization with llm_models relationship
        stmt = (
            select(Organization)
            .options(selectinload(Organization.llm_models))
            .filter(Organization.id == self.id)
        )
        
        result = await db.execute(stmt)
        org = result.scalar_one()
        
        # First try to find an enabled default model
        for model in org.llm_models:
            if model.is_default and model.is_enabled:
                return model
        
        # If no enabled default found, return first enabled model
        for model in org.llm_models:
            if model.is_enabled:
                return model
                
        return None
    
    async def get_subscription(self, db):
        return None
    
    async def get_completions(self, db):
        stmt = (
            select(Report)
            .join(Report.completions)
            .where(Report.organization_id == self.id)
        )
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_settings(self, db):
        """Get organization settings, creating them if they don't exist.
        
        Args:
            db: AsyncSession instance
        
        Returns:
            OrganizationSettings: The organization settings object
        """
        from app.models.organization_settings import OrganizationSettings
        
        # Try to load settings from the database
        stmt = select(OrganizationSettings).filter(OrganizationSettings.organization_id == self.id)
        result = await db.execute(stmt)
        settings = result.scalar_one_or_none()
        # If no settings exist, create them
        if settings is None:
            settings = OrganizationSettings(organization_id=self.id)
            db.add(settings)
            await db.flush()
            self.settings = settings
        
        return settings

from app.models.membership import Membership
from app.models.memory import Memory
from app.models.organization_settings import OrganizationSettings
