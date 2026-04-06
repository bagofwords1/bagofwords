from pydantic import BaseModel
from typing import List, Optional


# --- Roles ---

class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    permissions: List[str] = []


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[List[str]] = None


class RoleSchema(RoleCreate):
    id: str
    organization_id: Optional[str] = None
    is_system: bool = False

    class Config:
        from_attributes = True


# --- Groups ---

class GroupCreate(BaseModel):
    name: str
    description: Optional[str] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class GroupSchema(GroupCreate):
    id: str
    external_id: Optional[str] = None
    external_provider: Optional[str] = None
    member_count: int = 0

    class Config:
        from_attributes = True


class GroupMemberAdd(BaseModel):
    user_id: str


class GroupMemberSchema(BaseModel):
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None

    class Config:
        from_attributes = True


# --- Role Assignments ---

class RoleAssignmentCreate(BaseModel):
    role_id: str
    principal_type: str  # "user" | "group"
    principal_id: str


class RoleAssignmentSchema(RoleAssignmentCreate):
    id: str
    organization_id: str
    role: Optional[RoleSchema] = None

    class Config:
        from_attributes = True


# --- Resource Grants ---

class ResourceGrantCreate(BaseModel):
    resource_type: str  # "data_source" | "connection"
    resource_id: str
    principal_type: str  # "user" | "group"
    principal_id: str
    permissions: List[str] = []


class ResourceGrantUpdate(BaseModel):
    permissions: List[str]


class ResourceGrantSchema(ResourceGrantCreate):
    id: str
    organization_id: str

    class Config:
        from_attributes = True
