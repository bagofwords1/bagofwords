from sqlalchemy import Column, String, DateTime, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


# Constants for principal types
PRINCIPAL_TYPE_USER = "user"
PRINCIPAL_TYPE_GROUP = "group"  # For future use


class DataSourceMembership(BaseSchema):
    __tablename__ = "data_source_memberships"
    
    data_source_id = Column(String(36), ForeignKey('data_sources.id'), nullable=False)
    principal_type = Column(String, nullable=False)  # "user" or "group"
    principal_id = Column(String(36), nullable=False)  # user_id or group_id
    config = Column(JSON, nullable=True)  # For future row-level access configuration
    
    # Relationships
    data_source = relationship("DataSource", back_populates="data_source_memberships", lazy="selectin")
    
    
    # Ensure unique membership per principal per data source
    __table_args__ = (
        UniqueConstraint('data_source_id', 'principal_type', 'principal_id', 
                        name='uq_data_source_membership'),
    )
    
    def __repr__(self):
        return f"<DataSourceMembership(data_source_id={self.data_source_id}, principal_type={self.principal_type}, principal_id={self.principal_id})>"