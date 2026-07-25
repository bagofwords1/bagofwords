from pydantic import BaseModel, field_validator
from typing import List, Optional, Literal
from datetime import datetime

from app.schemas.user_schema import UserSchema


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Project name cannot be empty")
        if len(v) > 120:
            raise ValueError("Project name is too long (max 120 characters)")
        return v


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    access: Optional[Literal["private", "org"]] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Project name cannot be empty")
        if len(v) > 120:
            raise ValueError("Project name is too long (max 120 characters)")
        return v


class ProjectMiniSchema(BaseModel):
    """Lightweight embed for ReportSchema (the prompt-box chip and lists)."""
    id: str
    name: str
    color: Optional[str] = None

    class Config:
        from_attributes = True


class ProjectSchema(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    access: Literal["private", "org"] = "private"
    user_id: str
    user: Optional[UserSchema] = None
    created_at: datetime
    updated_at: datetime
    # Per-caller context, filled by the service.
    report_count: int = 0
    member_count: int = 0
    is_owner: bool = False
    can_manage: bool = False

    class Config:
        from_attributes = True


class ProjectListResponse(BaseModel):
    projects: List[ProjectSchema]


class ProjectMemberSchema(BaseModel):
    """A user who has been granted access to a project."""
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    permissions: List[str] = []


class ProjectMemberUpsert(BaseModel):
    user_id: str
    permissions: List[Literal["view", "manage"]] = ["view"]

    @field_validator("permissions")
    @classmethod
    def at_least_view(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("At least one permission is required")
        return v


class ReportMoveRequest(BaseModel):
    """Bulk move: assign (or clear) the project for a set of owned reports."""
    report_ids: List[str]
    project_id: Optional[str] = None  # None/empty clears back to the root list
