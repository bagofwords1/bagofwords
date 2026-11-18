from pydantic import BaseModel
from typing import List
from app.schemas.user_schema import UserRead
from app.schemas.organization_schema import OrganizationAndRoleSchema

class UserProfileSchema(UserRead):
    # Output-only: echo the stored address as-is. Service-account principals
    # carry a synthetic ``svc.<id>@service.invalid`` address that EmailStr
    # rejects (special-use TLD), which made /users/whoami 500 for API keys.
    email: str
    organizations: List[OrganizationAndRoleSchema] = []
    # Whether this user may create another organization on this deployment
    # (instance administrator, multiple organizations enabled, licensed). The
    # UI uses it to show "Create organization"; the API enforces the same rule.
    can_create_organization: bool = False
    # Whether this user may open instance-wide settings (license, system logs,
    # product updates, sites allowed to frame the app): an instance
    # administrator, or anyone while the instance has and can have only one
    # organization (the per-organization permission still applies there).
    is_instance_settings_admin: bool = False
