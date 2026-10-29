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
