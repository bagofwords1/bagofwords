# SCIM 2.0 Schemas
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field


# --- SCIM Core Schemas (RFC 7643) ---

class ScimMeta(BaseModel):
    resourceType: str
    created: Optional[datetime] = None
    lastModified: Optional[datetime] = None
    location: Optional[str] = None


class ScimName(BaseModel):
    formatted: Optional[str] = None
    givenName: Optional[str] = None
    familyName: Optional[str] = None


class ScimEmail(BaseModel):
    value: str
    type: str = "work"
    primary: bool = True


class ScimUser(BaseModel):
    schemas: List[str] = ["urn:ietf:params:scim:schemas:core:2.0:User"]
    id: Optional[str] = None
    externalId: Optional[str] = None
    userName: str
    name: Optional[ScimName] = None
    displayName: Optional[str] = None
    emails: Optional[List[ScimEmail]] = None
    active: bool = True
    meta: Optional[ScimMeta] = None


class ScimUserCreate(BaseModel):
    schemas: List[str] = ["urn:ietf:params:scim:schemas:core:2.0:User"]
    externalId: Optional[str] = None
    userName: str
    name: Optional[ScimName] = None
    displayName: Optional[str] = None
    emails: Optional[List[ScimEmail]] = None
    active: bool = True


class ScimPatchOperation(BaseModel):
    op: str  # "add", "replace", "remove"
    path: Optional[str] = None
    value: Optional[Any] = None


class ScimPatchOp(BaseModel):
    schemas: List[str] = ["urn:ietf:params:scim:api:messages:2.0:PatchOp"]
    Operations: List[ScimPatchOperation]


class ScimListResponse(BaseModel):
    schemas: List[str] = ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    totalResults: int
    startIndex: int = 1
    itemsPerPage: int = 100
    Resources: List[ScimUser] = []


# --- SCIM Group Schemas (RFC 7643 §4.2) ---

SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"


class ScimGroupMember(BaseModel):
    # IdPs send `"$ref": null` (Entra) or omit it; only `value` is meaningful.
    model_config = {"populate_by_name": True}

    value: str
    display: Optional[str] = None
    ref: Optional[str] = Field(default=None, alias="$ref")
    type: Optional[str] = None


class ScimGroup(BaseModel):
    model_config = {"populate_by_name": True}

    schemas: List[str] = [SCIM_GROUP_SCHEMA]
    id: str
    externalId: Optional[str] = None
    displayName: str
    # None (not []) when the caller excluded members, so the key is dropped
    # from the response instead of claiming the group is empty.
    members: Optional[List[ScimGroupMember]] = None
    meta: Optional[ScimMeta] = None


class ScimGroupCreate(BaseModel):
    """POST/PUT body. Extra keys (Entra's `meta`, extension schemas) are ignored."""
    schemas: List[str] = [SCIM_GROUP_SCHEMA]
    externalId: Optional[str] = None
    displayName: str = Field(min_length=1, max_length=255)
    members: Optional[List[ScimGroupMember]] = None


class ScimGroupListResponse(BaseModel):
    schemas: List[str] = ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    totalResults: int
    startIndex: int = 1
    itemsPerPage: int = 100
    Resources: List[ScimGroup] = []


class ScimError(BaseModel):
    schemas: List[str] = ["urn:ietf:params:scim:api:messages:2.0:Error"]
    status: str
    detail: Optional[str] = None
    scimType: Optional[str] = None


# --- Token Management Schemas ---

class ScimTokenCreate(BaseModel):
    name: str = "SCIM Token"
    expires_at: Optional[datetime] = None


class ScimTokenResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ScimTokenCreated(ScimTokenResponse):
    """Returned only on creation - includes the full token (shown once)."""
    token: str
