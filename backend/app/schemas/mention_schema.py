from pydantic import BaseModel
from typing import List, Optional, Literal
from enum import Enum
from datetime import datetime


# ============================================
# Data Source Mention (from DataSource model)
# ============================================
class DataSourceMention(BaseModel):
    id: str
    type: Literal['data_source'] = 'data_source'
    name: str
    data_source_type: str  # From DataSource.type (postgres, snowflake, etc.)
    
    # Real fields from DataSource model
    description: Optional[str] = None  # DataSource.description
    is_active: bool
    is_public: Optional[bool] = None
    auth_policy: str  # system_only, user_required
    
    class Config:
        from_attributes = True


# ============================================
# Table Mention (from DataSourceTable model)
# ============================================
class TableMention(BaseModel):
    id: str
    type: Literal['datasource_table'] = 'datasource_table'
    name: str
    
    # Real fields from DataSourceTable model
    datasource_id: str
    columns: List[dict]  # Column names/types from DataSourceTable.columns
    is_active: bool
    
    # Computed/joined fields (need to join DataSource)
    data_source_name: str  # Join from DataSource.name
    data_source_type: str  # Join from DataSource.type
    
    class Config:
        from_attributes = True


# ============================================
# File Mention (from File model)
# ============================================
class FileMention(BaseModel):
    id: str
    type: Literal['file'] = 'file'
    filename: str  # File.filename (this is the display name)
    
    # Real fields from File model
    content_type: str  # File.content_type
    path: str
    created_at: datetime
    
    class Config:
        from_attributes = True


# ============================================
# Entity Mention (from Entity model)
# ============================================
class EntityMention(BaseModel):
    id: str
    type: Literal['entity'] = 'entity'
    title: str  # Entity.title (display name)
    
    # Real fields from Entity model
    description: Optional[str] = None
    
    # Relationships
    data_source_ids: List[str]  # From entity.data_sources relationship
    
    class Config:
        from_attributes = True


# ============================================
# Response wrapper
# ============================================
class AvailableMentionsResponse(BaseModel):
    data_sources: List[DataSourceMention]
    tables: List[TableMention]
    files: List[FileMention]
    entities: List[EntityMention]


# =====================================================
# Tracking schemas (for storing mentions on completions)
# =====================================================

class MentionType(str, Enum):
    FILE = "FILE"
    MEMORY = "MEMORY"  # kept for backward compatibility
    DATA_SOURCE = "DATA_SOURCE"
    TABLE = "TABLE"            # DataSourceTable
    ENTITY = "ENTITY"


class MentionBase(BaseModel):
    type: MentionType
    report_id: str
    object_id: str
    mention_content: str
    completion_id: str


class MentionCreate(MentionBase):
    pass


class MentionUpdate(BaseModel):
    type: Optional[MentionType] = None
    mention_content: Optional[str] = None
    object_id: Optional[str] = None
