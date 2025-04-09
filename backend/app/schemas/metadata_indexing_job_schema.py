from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum
from app.schemas.metadata_resource_schema import MetadataResourceSchema

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MetadataIndexingJobSchema(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    
    # Job details
    job_type: str = "dbt"
    status: JobStatus = JobStatus.PENDING
    error_message: Optional[str] = None
    
    # Configuration for the job
    config: Optional[Dict[str, Any]] = None

    resources: List[MetadataResourceSchema]
    
    # Statistics
    resources_processed: int = 0
    resources_failed: int = 0
    
    # Timing information
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # The data source this job is processing
    data_source_id: str
    
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        orm_mode = True


class MetadataIndexingJobCreate(BaseModel):
    name: str
    description: Optional[str] = None
    job_type: str = "dbt"
    config: Optional[Dict[str, Any]] = None
    data_source_id: str


class MetadataIndexingJobUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[JobStatus] = None
    error_message: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    resources_processed: Optional[int] = None
    resources_failed: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class MetadataIndexingJobResponse(MetadataIndexingJobSchema):
    pass


class MetadataIndexingJobList(BaseModel):
    items: List[MetadataIndexingJobSchema]
    total: int