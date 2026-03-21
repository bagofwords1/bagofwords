# SCIM 2.0 Schemas
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

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
