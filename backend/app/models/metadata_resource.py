from sqlalchemy import Column, String, Boolean, JSON, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class MetadataResource(BaseSchema):
    __tablename__ = "metadata_resources"

    # Basic information
    name = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)  # e.g., dbt model, dbt source, lookml model, lookml view
    path = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    
    # Raw data from the extractor
    raw_data = Column(JSON, nullable=True)  # Store the complete resource data as JSON
    
    # SQL content for models, tests, etc.
    sql_content = Column(Text, nullable=True)
    
    # For sources (DBT specific, might generalize later)
    source_name = Column(String, nullable=True)
    database = Column(String, nullable=True)
    schema = Column(String, nullable=True)
    
    # Common fields (can store LookML dimensions/measures, DBT columns etc.)
    columns = Column(JSON, nullable=True)  # Store column/field definitions as JSON
    depends_on = Column(JSON, nullable=True)
    
    # Status and tracking
    is_active = Column(Boolean, nullable=False, default=True)
    last_synced_at = Column(DateTime, nullable=True)
    
    # The data source this resource belongs to
    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=False)

    # The job that indexed this resource version
    metadata_indexing_job_id = Column(String(36), ForeignKey("metadata_indexing_jobs.id"), nullable=True)

    
    # Relationships
    data_source = relationship("DataSource", back_populates="metadata_resources")
    metadata_indexing_job = relationship("MetadataIndexingJob", back_populates="metadata_resources")
    
    def __repr__(self):
        return f"<MetadataResource {self.resource_type}:{self.name}>"