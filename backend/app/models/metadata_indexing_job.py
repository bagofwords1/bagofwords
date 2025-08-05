from sqlalchemy import Column, String, Boolean, JSON, DateTime, Text, ForeignKey, Integer
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.models.base import BaseSchema
from enum import Enum as PyEnum


class IndexingJobType(str, PyEnum):
    DBT = "dbt"
    LOOKER = "looker"
    TABLEAU = "tableau"
    POWERBI = "powerbi"
    MARKDOWN = "markdown"
    CUSTOM = "custom"


class IndexingJobStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MetadataIndexingJob(BaseSchema):
    __tablename__ = "metadata_indexing_jobs"

    # Basic information
    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    git_repository_id = Column(String(36), ForeignKey("git_repositories.id"), nullable=True)

    job_type = Column(String, nullable=False, default=IndexingJobType.DBT.value)
    status = Column(String, nullable=False, default=IndexingJobStatus.PENDING.value)

    detected_project_types = Column(JSON, nullable=True) # List of strings, e.g., ["dbt", "lookml"]
    
    # Job details
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # Statistics
    total_resources = Column(Integer, nullable=True)
    processed_resources = Column(Integer, nullable=True, default=0)
    
    # Error tracking
    error_message = Column(Text, nullable=True)
    error_details = Column(JSON, nullable=True)
    
    # Configuration and results
    config = Column(JSON, nullable=True)  # Job configuration parameters
    summary = Column(JSON, nullable=True)  # Summary of indexed resources by type
    
    # Raw data from the extraction process
    raw_data = Column(JSON, nullable=True)  # Complete extracted data before processing

    # Detected project types in the repository for this job
    detected_project_types = Column(JSON, nullable=True) # List of strings, e.g., ["dbt", "lookml"]

    is_active = Column(Boolean, nullable=False, default=True)
    
    # Relationships
    data_source = relationship("DataSource", back_populates="metadata_indexing_jobs")
    git_repository = relationship("GitRepository", back_populates="metadata_indexing_jobs")
    
    # Use lambda for late binding to avoid circular imports
    metadata_resources = relationship(
        lambda: MetadataResource,
        back_populates="metadata_indexing_job"
    )

    def __repr__(self):
        return f"<MetadataIndexingJob {self.job_type}:{self.id} - {self.status}>"
    
    def start(self):
        """Mark the job as started"""
        self.status = IndexingJobStatus.RUNNING.value
        self.started_at = func.now()
    
    def complete(self, total_resources, raw_data=None):
        """Mark the job as completed"""
        self.status = IndexingJobStatus.COMPLETED.value
        self.completed_at = func.now()
        self.total_resources = total_resources
        self.processed_resources = total_resources
        if raw_data is not None:
            self.raw_data = raw_data
    
    def fail(self, error_message, error_details=None):
        """Mark the job as failed"""
        self.status = IndexingJobStatus.FAILED.value
        self.completed_at = func.now()
        self.error_message = error_message
        self.error_details = error_details
    
    def cancel(self):
        """Mark the job as cancelled"""
        self.status = IndexingJobStatus.CANCELLED.value
        self.completed_at = func.now()
    
    def update_progress(self, processed_resources, total_resources=None, partial_raw_data=None):
        """Update job progress"""
        self.processed_resources = processed_resources
        if total_resources is not None:
            self.total_resources = total_resources
        if partial_raw_data is not None:
            # Merge with existing raw_data if it exists
            if self.raw_data is None:
                self.raw_data = partial_raw_data
            else:
                # This assumes raw_data is a dictionary with resource types as keys
                for resource_type, resources in partial_raw_data.items():
                    if resource_type not in self.raw_data:
                        self.raw_data[resource_type] = resources
                    else:
                        self.raw_data[resource_type].extend(resources)

# Import at the end to avoid circular imports
from app.models.metadata_resource import MetadataResource 