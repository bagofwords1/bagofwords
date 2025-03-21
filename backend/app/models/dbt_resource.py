from sqlalchemy import Column, String, Boolean, JSON, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class DBTResource(BaseSchema):
    __tablename__ = "dbt_resources"

    # Basic information
    name = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)  # model, source, metric, seed, macro, test, exposure
    path = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    
    # Raw data from the extractor
    raw_data = Column(JSON, nullable=True)  # Store the complete resource data as JSON
    
    # SQL content for models, tests, etc.
    sql_content = Column(Text, nullable=True)
    
    # For sources
    source_name = Column(String, nullable=True)
    database = Column(String, nullable=True)
    schema = Column(String, nullable=True)
    
    # Common fields
    columns = Column(JSON, nullable=True)  # Store column definitions as JSON
    depends_on = Column(JSON, nullable=True)
    
    # Status and tracking
    is_active = Column(Boolean, nullable=False, default=True)
    last_synced_at = Column(DateTime, nullable=True)
    
    # The data source this resource belongs to
    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=False)

    metadata_indexing_job_id = Column(String(36), ForeignKey("metadata_indexing_jobs.id"), nullable=True)

    
    # Relationships
    data_source = relationship("DataSource", back_populates="dbt_resources")
    metadata_indexing_job = relationship("MetadataIndexingJob", back_populates="dbt_resources")
    
    def __repr__(self):
        return f"<DBTResource {self.resource_type}:{self.name}>"