"""Host applications (Settings -> Integrations) and an artifact's embedding."""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

MAX_ORIGINS = 20
IdentityMode = Literal["assertion", "idp_token", "sso"]
_SOURCE_RE = re.compile(r"^(oidc:[A-Za-z0-9_.-]{1,64}|saml:[a-z][a-z0-9-]{0,47}|ldap|scim|member_attribute:[A-Za-z][A-Za-z0-9_.-]{0,63})$")


def normalize_origin(value: str) -> str:
    """``https://erp.example.com[:port]`` or ValueError: an origin is what the
    browser compares, so no path, query, fragment or credentials."""
    raw = (value or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"'{raw}' is not an origin (expected scheme://host[:port])")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError(f"'{raw}' must be a bare origin without path, query or credentials")
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"'{raw}' has no host")
    default_port = 80 if parts.scheme == "http" else 443
    port = f":{parts.port}" if parts.port and parts.port != default_port else ""
    return f"{parts.scheme}://{host.lower()}{port}"


def _origins(value):
    seen: list[str] = []
    for item in value or []:
        origin = normalize_origin(item)
        if origin not in seen:
            seen.append(origin)
    if len(seen) > MAX_ORIGINS:
        raise ValueError(f"at most {MAX_ORIGINS} origins")
    return seen


def _source(value):
    if value is None or value == "":
        return None
    value = value.strip()
    if not _SOURCE_RE.match(value):
        raise ValueError("identity_source must be oidc:<provider>, saml:<provider>, ldap, scim or member_attribute:<key>")
    return value


class HostApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    allowed_origins: List[str] = Field(default_factory=list)
    identity_mode: IdentityMode = "assertion"
    identity_source: Optional[str] = None
    create_users: bool = False
    issuer: Optional[str] = Field(default=None, max_length=255)
    audience: Optional[str] = Field(default=None, max_length=255)
    enabled: bool = True

    @field_validator("allowed_origins")
    @classmethod
    def _o(cls, v):
        return _origins(v)

    @field_validator("identity_source")
    @classmethod
    def _s(cls, v):
        return _source(v)

    @field_validator("name", "issuer", "audience")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class HostApplicationUpdate(BaseModel):
    """Partial update; ``revision`` must match the row the caller edited."""
    revision: int
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    allowed_origins: Optional[List[str]] = None
    identity_mode: Optional[IdentityMode] = None
    identity_source: Optional[str] = None
    create_users: Optional[bool] = None
    issuer: Optional[str] = Field(default=None, max_length=255)
    audience: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None

    @field_validator("allowed_origins")
    @classmethod
    def _o(cls, v):
        return None if v is None else _origins(v)

    @field_validator("identity_source")
    @classmethod
    def _s(cls, v):
        return None if v is None else _source(v)


class HostApplicationSchema(BaseModel):
    id: str
    public_id: str
    name: str
    enabled: bool
    allowed_origins: List[str]
    identity_mode: IdentityMode
    identity_source: Optional[str] = None
    create_users: bool
    issuer: Optional[str] = None
    audience: Optional[str] = None
    key_fingerprint: Optional[str] = None
    epoch: int
    revision: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    active_sessions: int = 0
    artifacts: int = 0


class HostApplicationKeySchema(HostApplicationSchema):
    """Create / rotate-key response: the only time the private key is readable."""
    private_key: Optional[str] = None
    public_key: Optional[str] = None


class IdentitySourceOption(BaseModel):
    value: str
    label: str
    kind: Literal["oidc", "saml", "ldap", "scim", "member_attribute"]
    supports_create_users: bool
    supports_idp_token: bool = False


class EmbedSessionRequest(BaseModel):
    """Exactly one credential, matching the host application's identity mode."""
    assertion: Optional[str] = Field(default=None, max_length=8192)
    id_token: Optional[str] = Field(default=None, max_length=32768)
    sso_code: Optional[str] = Field(default=None, max_length=256)
    code_verifier: Optional[str] = Field(default=None, max_length=256)


class EmbedSessionSchema(BaseModel):
    token: str
    expires_at: datetime
    user: dict
    host_application: str
    organization_id: str
    # ARTIFACT_DATA.current_user for the embedded artifact (users/me/viewer_context's shape).
    viewer: dict


class EmbedBootstrapSchema(BaseModel):
    """Public: what the embedded page needs before anyone is signed in."""
    host_application: str
    name: str
    enabled: bool
    allowed_origins: List[str]
    identity_mode: IdentityMode


class SsoCodeRequest(BaseModel):
    code_challenge: str = Field(min_length=43, max_length=128)


class ArtifactEmbeddingUpdate(BaseModel):
    host_application_ids: List[str] = Field(default_factory=list, max_length=50)


class ArtifactEmbeddingHost(BaseModel):
    id: str
    public_id: str
    name: str
    enabled: bool
    allowed_origins: List[str]
    identity_mode: IdentityMode
    # For the backend example on the Embed tab (neither is secret).
    identity_source: Optional[str] = None
    issuer: Optional[str] = None
    allowed: bool


class ArtifactEmbeddingSchema(BaseModel):
    artifact_id: str
    visibility: str
    hosts: List[ArtifactEmbeddingHost]
    can_manage_hosts: bool
    sdk_path: str = "/api/embed/sdk/v1/bow-embed.js"
