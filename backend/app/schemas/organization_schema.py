from pydantic import BaseModel, Field
from uuid import UUID
from typing import Dict, List, Optional
from app.schemas.user_schema import UserSchema
from app.schemas.usage_policy_schema import UsageQuotaSummarySchema

# Per-org note about a member. Surfaced to the AI planner. Capped to keep
# prompt overhead negligible.
MEMBERSHIP_NOTE_MAX_LENGTH = 500

class OrganizationCreate(BaseModel):
    name: str
    description: Optional[str] = None

class OrganizationSchema(OrganizationCreate):
    id: str

    class Config:
        from_attributes = True

class MembershipCreate(BaseModel):
    organization_id: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = "member"
    note: Optional[str] = Field(default=None, max_length=MEMBERSHIP_NOTE_MAX_LENGTH)

class MembershipUpdate(BaseModel):
    role: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=MEMBERSHIP_NOTE_MAX_LENGTH)


# Import row outcomes returned to the caller. Mirrors what the UI shows
# in the preview / commit report.
class MembershipImportRow(BaseModel):
    row: int  # 1-based source row (header is row 1, first data row is row 2)
    email: Optional[str] = None
    note: Optional[str] = None
    status: str  # "created" | "updated" | "unchanged" | "error"
    error: Optional[str] = None


class MembershipImportSummary(BaseModel):
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0


class MembershipImportReport(BaseModel):
    dry_run: bool
    summary: MembershipImportSummary
    rows: List[MembershipImportRow]

class RoleSummarySchema(BaseModel):
    id: str
    name: str
    source: str = "direct"  # "direct" or "group:<group_name>"

    class Config:
        from_attributes = True


class MembershipSchema(MembershipCreate):
    id: str
    user: Optional[UserSchema] = None
    email: Optional[str] = None
    roles: List[RoleSummarySchema] = []  # resolved from role_assignments

    class Config:
        from_attributes = True


class OrganizationAndRoleSchema(OrganizationSchema):
    role: str  # backward compat — first/primary role name
    roles: List[str] = []  # all assigned role names
    permissions: List[str] = []  # resolved org permission union
    resource_permissions: Dict[str, List[str]] = {}  # "data_source:<id>" -> ["query", ...]
    is_enterprise: bool = False  # whether enterprise license is active
    icon_url: Optional[str] = None
    ai_analyst_name: Optional[str] = None
    usage_quota: Optional[UsageQuotaSummarySchema] = None

class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
