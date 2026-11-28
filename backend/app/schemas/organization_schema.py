from pydantic import BaseModel, EmailStr, Field, field_validator
from uuid import UUID
from datetime import datetime
from typing import Dict, List, Optional
from app.schemas.user_schema import UserSchema
from app.schemas.usage_policy_schema import UsageQuotaSummarySchema
from app.schemas.base import OptionalUTCDatetime

# Per-org note about a member. Surfaced to the AI planner. Capped to keep
# prompt overhead negligible.
MEMBERSHIP_NOTE_MAX_LENGTH = 500

ORGANIZATION_NAME_MAX_LENGTH = 100

class OrganizationBase(BaseModel):
    name: str
    description: Optional[str] = None

class OrganizationCreate(OrganizationBase):
    # Optional: invite this address as an admin of the new organization. It is
    # an ordinary invite (same email, same acceptance) as one sent from the
    # members page. The creator joins as admin either way.
    first_admin_email: Optional[EmailStr] = None

    @field_validator("first_admin_email", mode="before")
    @classmethod
    def _blank_first_admin_email_is_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

class OrganizationSchema(OrganizationBase):
    id: str
    # Set only in the response to a creation that asked for a first admin:
    # "sent" | "failed" | "skipped_no_smtp" (invite email outcome), "added"
    # (the address already had an account that could be attached directly),
    # "already_member" (it is the creator) or "not_invited" (the invite could
    # not be created; the organization was).
    first_admin_invite_status: Optional[str] = None

    class Config:
        from_attributes = True

class MembershipCreate(BaseModel):
    organization_id: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    # None = caller didn't pick a role; add_member resolves it to the org-wide
    # default_member_role (which itself defaults to "member"). An explicit value
    # (e.g. chosen in the Add-member modal) is used as-is.
    role: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=MEMBERSHIP_NOTE_MAX_LENGTH)
    # Admin-set attributes identifying the member in other systems (e.g.
    # {"qlik_user_id": "dana"}); consumed by connection identity mappings.
    # Validated by services/member_attributes.normalize_custom_attributes.
    custom_attributes: Optional[Dict[str, str | list[str]]] = None
    # Roles (system or custom) and groups to give the new member, applied in
    # the same transaction as the invite. When role_ids is non-empty it wins
    # over `role` (which is then derived from it for the legacy column).
    # Groups need the enterprise `custom_roles` feature. Request-only: these
    # are not columns on Membership.
    role_ids: Optional[List[str]] = None
    group_ids: Optional[List[str]] = None

class MembershipUpdate(BaseModel):
    role: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=MEMBERSHIP_NOTE_MAX_LENGTH)
    # Replaces the member's whole attribute set when present ({} clears it).
    custom_attributes: Optional[Dict[str, str | list[str] | None]] = None


# Import row outcomes returned to the caller. Mirrors what the UI shows
# in the preview / commit report.
class MembershipImportRow(BaseModel):
    row: int  # 1-based source row (header is row 1, first data row is row 2)
    email: Optional[str] = None
    note: Optional[str] = None
    # Attributes this row sets (from attr.<key> columns); merged into the
    # member's existing attributes, never replacing ones the file omits.
    custom_attributes: Optional[Dict[str, str | list[str]]] = None
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
    # Outcome of the invite email on creation: "sent" | "failed" |
    # "skipped_no_smtp" | None (not an invite / not applicable).
    invite_email_status: Optional[str] = None
    # When the pending invite link expires (pending invites only).
    invite_expires_at: OptionalUTCDatetime = None
    # The host application whose sign-in created this membership (an app
    # user's first arrival through an embedded artifact), and its name.
    created_via_host_application_id: Optional[str] = None
    created_via_host_application: Optional[str] = None

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
