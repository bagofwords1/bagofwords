# Audit Log Schemas
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime


class AuditLogBase(BaseModel):
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: Optional[dict] = None


class AuditLogCreate(AuditLogBase):
    """Schema for creating audit log entries (internal use)"""
    organization_id: str
    user_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None


class AuditLogResponse(AuditLogBase):
    """Schema for audit log API responses"""
    id: str
    organization_id: str
    user_id: Optional[str] = None
    user_email: Optional[str] = None  # Populated from relationship
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AuditLogListResponse(BaseModel):
    """Paginated list of audit logs"""
    items: List[AuditLogResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class AuditLogFilters(BaseModel):
    """Filters for querying audit logs"""
    action: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    user_id: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    search: Optional[str] = None
