from pydantic import BaseModel, Field, field_validator
from typing import List, Optional


# --- Roles ---

class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    permissions: List[str] = []

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Role name cannot be empty")
        return v


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    permissions: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Role name cannot be empty")
        return v


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
