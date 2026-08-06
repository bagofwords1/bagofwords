from pydantic import BaseModel
from typing import List
from app.schemas.user_schema import UserRead
from app.schemas.organization_schema import OrganizationAndRoleSchema

class UserProfileSchema(UserRead):
    # ``UserRead`` inherits ``email: EmailStr`` from fastapi-users. Service
    # accounts carry a synthetic, deliberately-undeliverable address
    # (``svc.<id>@service.invalid``, see ServiceAccountService) which
    # email-validator rejects because ``.invalid`` is a reserved TLD — so
    # validating it here 500s ``GET /users/whoami`` for every service-account
    # principal, the exact call an API integration makes to check its key.
    # This is a response schema: the address is echoed back, never parsed.
    email: str
    organizations: List[OrganizationAndRoleSchema] = []
