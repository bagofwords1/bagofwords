from sqlalchemy import Column, String
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.report import Report
from app.models.git_repository import GitRepository

class Organization(BaseSchema):
    __tablename__ = "organizations"
    
    name = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    
    memberships = relationship("Membership", back_populates="organization")
    reports = relationship("Report", back_populates="organization")
    users = relationship("User", secondary="memberships", back_populates="organizations")
    files = relationship("File", back_populates="organization")
    data_sources = relationship("DataSource", back_populates="organization")
    memories = relationship("Memory", back_populates="organization")
    llm_providers = relationship("LLMProvider", back_populates="organization")
    llm_models = relationship("LLMModel", back_populates="organization")
    git_repositories = relationship("GitRepository", back_populates="organization")
    
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


from app.models.membership import Membership
from app.models.memory import Memory
