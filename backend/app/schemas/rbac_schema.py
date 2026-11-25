from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Literal, Optional


# --- Roles ---

# Group-manager grants (resource_type "group") are written only by the
# promote/demote routes, which hold the rules for who may carry one: a single
# user who is a member of that group. Accepting them through the generic grant
# or role routes would let a role or a whole group administer a group.
GROUP_RESOURCE_TYPE = "group"
# LLM provider managers (resource_type "llm_provider", permissions ["manage"])
# are likewise written only by PUT/DELETE /llm/providers/{id}/managers/{user},
# which require org-wide manage_llm and a single org member as the principal.
LLM_PROVIDER_RESOURCE_TYPE = "llm_provider"
DEDICATED_GRANT_RESOURCE_TYPES = (GROUP_RESOURCE_TYPE, LLM_PROVIDER_RESOURCE_TYPE)


def _reject_group_grants(value: str) -> str:
    if value == GROUP_RESOURCE_TYPE:
        raise ValueError("Group managers are set on the group itself, not through grants")
    if value == LLM_PROVIDER_RESOURCE_TYPE:
        raise ValueError("Provider managers are set on the LLM provider itself, not through grants")
    return value


class RoleResourceGrantInput(BaseModel):
    resource_type: str  # "data_source" | "connection"
    resource_id: str
    permissions: List[str] = []

    @field_validator("resource_type")
    @classmethod
    def reject_group_grants(cls, v: str) -> str:
        return _reject_group_grants(v)


class RoleResourceGrantOutput(RoleResourceGrantInput):
    @field_validator("resource_type")
    @classmethod
    def reject_group_grants(cls, v: str) -> str:
        return v


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
    # Base system-role name this row is an editable per-org override of
    # (e.g. "member"); None for the global system rows and ordinary custom roles.
    overrides_system_role: Optional[str] = None
    # UI capability flags derived server-side: a built-in `member` row is
    # editable (editing it materializes an override) but never deletable.
    is_editable: bool = True
    is_deletable: bool = True

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
    # Users who manage this group's membership (see group managers in
    # app/services/rbac_service.py). Always empty for imported groups.
    manager_user_ids: List[str] = []

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
    is_manager: bool = False

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
    # Per-resource grants target people. Role-wide grants are written only by
    # the role editor (org admin), so "role" is not accepted here.
    principal_type: Literal["user", "group"]
    principal_id: str
    permissions: List[str] = []

    @field_validator("resource_type")
    @classmethod
    def reject_group_grants(cls, v: str) -> str:
        return _reject_group_grants(v)


class ResourceGrantUpdate(BaseModel):
    permissions: List[str]


class ResourceGrantSchema(ResourceGrantCreate):
    principal_type: Literal["user", "group", "role"]
    id: str
    organization_id: str

    @field_validator("resource_type")
    @classmethod
    def reject_group_grants(cls, v: str) -> str:
        return v

    class Config:
        from_attributes = True
