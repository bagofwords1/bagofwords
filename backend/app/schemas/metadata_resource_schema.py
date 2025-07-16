from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime


class MetadataResourceSchema(BaseModel):
    id: Optional[str] = None
    name: str
    resource_type: str
    path: Optional[str] = None
    description: Optional[str] = None
    
    # Raw data from the extractor
    raw_data: Optional[Dict[str, Any]] = None
    
    # SQL content for models, tests, etc.
    sql_content: Optional[str] = None
    
    # For sources
    source_name: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    
    # Common fields
    columns: Optional[List[Dict[str, Any]]] = None
    depends_on: Optional[List[str]] = None
    
    # Status and tracking
    is_active: bool = True
    last_synced_at: Optional[datetime] = None
    
    # The data source this resource belongs to
    data_source_id: str
    
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        orm_mode = True


class MetadataResourceCreate(BaseModel):
    name: str
    resource_type: str
    path: Optional[str] = None
    description: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    sql_content: Optional[str] = None
    source_name: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    columns: Optional[List[Dict[str, Any]]] = None
    depends_on: Optional[List[str]] = None
    is_active: bool = True
    data_source_id: str
    metadata_indexing_job_id: Optional[str] = None


class MetadataResourceUpdate(BaseModel):
    name: Optional[str] = None
    resource_type: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    sql_content: Optional[str] = None
    source_name: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    columns: Optional[List[Dict[str, Any]]] = None
    depends_on: Optional[List[str]] = None
    is_active: Optional[bool] = None
    last_synced_at: Optional[datetime] = None


class MetadataResourceResponse(MetadataResourceSchema):
    pass


class MetadataResourceList(BaseModel):
    items: List[MetadataResourceSchema]
    total: int


