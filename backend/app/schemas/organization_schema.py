from pydantic import BaseModel
from uuid import UUID
from typing import Dict, List, Optional
from app.schemas.user_schema import UserSchema

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

class MembershipUpdate(BaseModel):
    role: Optional[str] = None

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
    icon_url: Optional[str] = None
    ai_analyst_name: Optional[str] = None

class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None