"""Embedded chat: admin configuration and public/session payloads.

Validation of anything that needs the database (agents, models) lives in
EmbedService; these models cover shape, bounds and vocabulary.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.schemas.report_api_settings import validate_tools
from app.utils.reasoning_effort import normalize_effort

MAX_ORIGINS = 20
MAX_SUGGESTIONS = 6
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Same vocabulary as member attribute keys (services/member_attributes.py):
# the subject attribute names one of them.
_ATTRIBUTE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

Theme = Literal["light", "dark", "auto"]


def normalize_origin(value: str) -> str:
    """``https://erp.example.com`` form or ValueError. No path, query, fragment
    or credentials: an origin is what the browser compares, nothing more."""
    raw = (value or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"'{raw}' is not an origin (expected scheme://host[:port])")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError(f"'{raw}' must be a bare origin without path, query or credentials")
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"'{raw}' has no host")
    port = f":{parts.port}" if parts.port and parts.port not in (80 if parts.scheme == "http" else 443,) else ""
    return f"{parts.scheme}://{host.lower()}{port}"


class EmbedRuntime(BaseModel):
    """What the chat runs with. An explicit roster: [] means NO agents, never
    'everything the user can reach' (see agent_focus_common.resolve_run_agents)."""
    data_source_ids: List[str] = Field(default_factory=list, max_length=50)
    model_id: Optional[str] = None  # llm_models.id; None = organization default
    reasoning_effort: Optional[str] = None
    tools: Optional[Dict[str, Any]] = None  # public switches {key: false}
    follow_ups: Optional[bool] = None
    # Off by default: the knowledge harness proposes admin instructions, which
    # an employee inside a host application has no surface to act on.
    suggest_instructions: Optional[bool] = False
    allow_agent_selection: bool = False

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _effort(cls, v):
        return normalize_effort(v)

    @field_validator("tools")
    @classmethod
    def _tools(cls, v):
        return validate_tools(v)

    @field_validator("data_source_ids")
    @classmethod
    def _ids(cls, v):
        cleaned = []
        for item in v or []:
            s = str(item or "").strip()
            if s and s not in cleaned:
                cleaned.append(s)
        return cleaned


class EmbedBranding(BaseModel):
    display_name: str = Field(default="", max_length=60)
    accent_color: str = "#2563eb"
    theme: Theme = "light"
    greeting: str = Field(default="", max_length=500)
    suggestions: List[str] = Field(default_factory=list, max_length=MAX_SUGGESTIONS)
    launcher_label: str = Field(default="", max_length=40)
    logo_key: Optional[str] = None  # set by the logo upload route, never by PUT
    # Fixed locale for the chat; None = whatever the host/browser asks for,
    # restricted to the enabled locales either way.
    locale: Optional[str] = Field(default=None, max_length=8)

    @field_validator("accent_color")
    @classmethod
    def _color(cls, v):
        v = (v or "").strip()
        if not _HEX_COLOR_RE.match(v):
            raise ValueError("accent_color must be a #rrggbb hex color")
        return v.lower()

    @field_validator("suggestions")
    @classmethod
    def _suggestions(cls, v):
        out = []
        for s in v or []:
            s = str(s or "").strip()
            if len(s) > 160:
                raise ValueError("each suggestion must be at most 160 characters")
            if s:
                out.append(s)
        return out


def _validate_origins(v):
    seen = []
    for item in v or []:
        o = normalize_origin(item)
        if o not in seen:
            seen.append(o)
    if len(seen) > MAX_ORIGINS:
        raise ValueError(f"at most {MAX_ORIGINS} origins")
    return seen


def _validate_attribute_key(v):
    v = (v or "").strip()
    if not _ATTRIBUTE_KEY_RE.match(v):
        raise ValueError("subject_attribute must be a member attribute key (letters, digits, _ . -)")
    return v


class EmbedCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    allowed_origins: List[str] = Field(default_factory=list)
    issuer: str = Field(min_length=1, max_length=255)
    subject_attribute: str
    enabled: bool = True
    runtime: EmbedRuntime = Field(default_factory=EmbedRuntime)
    branding: EmbedBranding = Field(default_factory=EmbedBranding)

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v):
        return _validate_origins(v)

    @field_validator("subject_attribute")
    @classmethod
    def _subject(cls, v):
        return _validate_attribute_key(v)

    @field_validator("issuer", "name")
    @classmethod
    def _strip(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class EmbedUpdate(BaseModel):
    """Partial update. ``revision`` is required: the edit must be based on the
    row the caller last saw (409 otherwise)."""
    revision: int
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    allowed_origins: Optional[List[str]] = None
    issuer: Optional[str] = Field(default=None, min_length=1, max_length=255)
    subject_attribute: Optional[str] = None
    enabled: Optional[bool] = None
    runtime: Optional[EmbedRuntime] = None
    branding: Optional[EmbedBranding] = None

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v):
        return None if v is None else _validate_origins(v)

    @field_validator("subject_attribute")
    @classmethod
    def _subject(cls, v):
        return None if v is None else _validate_attribute_key(v)


class EmbedSchema(BaseModel):
    id: str
    public_id: str
    name: str
    enabled: bool
    allowed_origins: List[str]
    issuer: str
    subject_attribute: str
    key_fingerprint: str
    epoch: int
    runtime: EmbedRuntime
    branding: EmbedBranding
    revision: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Operational counters for the settings page.
    mapped_members: int = 0
    active_sessions: int = 0
    conversations: int = 0


class EmbedCreatedSchema(EmbedSchema):
    """The create / rotate response: the only time the private key is readable."""
    private_key: str
    public_key: str


class EmbedSessionRequest(BaseModel):
    assertion: str = Field(min_length=16, max_length=8192)


class EmbedAgentSchema(BaseModel):
    id: str
    name: str


class EmbedSessionSchema(BaseModel):
    token: str
    expires_at: datetime
    user: Dict[str, Any]
    agents: List[EmbedAgentSchema]
    allow_agent_selection: bool = False
    embed_id: str


class EmbedBootstrapSchema(BaseModel):
    """Public: what an unauthenticated iframe needs to paint its first frame.
    No agents, no members, no runtime policy."""
    embed_id: str
    name: str
    enabled: bool
    allowed_origins: List[str]
    branding: Dict[str, Any]
    sdk_version: str = "1"


class EmbedChatCreate(BaseModel):
    """A message from the compact chat. Deliberately not CompletionCreate: the
    employee may send text, host context and request metadata — never a model,
    a platform, widget/step targets or follow-up switches."""
    content: str = Field(min_length=1, max_length=20000)
    context: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    data_source_ids: Optional[List[str]] = None  # only with allow_agent_selection
    idempotency_key: Optional[str] = Field(default=None, max_length=80)
