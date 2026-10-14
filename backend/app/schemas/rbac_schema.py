from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Optional


# --- Roles ---

class RoleResourceGrantInput(BaseModel):
    resource_type: str  # "data_source" | "connection"
    resource_id: str
    permissions: List[str] = []


class RoleResourceGrantOutput(RoleResourceGrantInput):
    pass


class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    permissions: List[str] = []
    resource_grants: List[RoleResourceGrantInput] = []

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
    resource_grants: Optional[List[RoleResourceGrantInput]] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Role name cannot be empty")
        return v


class RoleSchema(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    permissions: List[str] = []
    resource_grants: List[RoleResourceGrantOutput] = []
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
    member_user_ids: List[str] = []
    # Pending (unregistered) memberships pre-assigned to this group, keyed by
    # membership id. Materialized into ``member_user_ids`` when the invitee
    # registers.
    member_membership_ids: List[str] = []

    class Config:
        from_attributes = True


class GroupMemberAdd(BaseModel):
    # Exactly one of ``user_id`` (registered user) or ``membership_id``
    # (pending invite) must be provided.
    user_id: Optional[str] = None
    membership_id: Optional[str] = None

    @model_validator(mode="after")
    def exactly_one_principal(self) -> "GroupMemberAdd":
        if bool(self.user_id) == bool(self.membership_id):
            raise ValueError("Provide exactly one of user_id or membership_id")
        return self


class GroupMemberSchema(BaseModel):
    user_id: Optional[str] = None
    membership_id: Optional[str] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    pending: bool = False

    class Config:
        from_attributes = True


# --- Role Assignments ---

class RoleAssignmentCreate(BaseModel):
    role_id: str
    principal_type: str  # "user" | "group" | "membership" (pending invite)
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
