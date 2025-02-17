from pydantic import BaseModel
from typing import List
from app.schemas.user_schema import UserRead
from app.schemas.organization_schema import OrganizationAndRoleSchema

class UserProfileSchema(UserRead):
    organizations: List[OrganizationAndRoleSchema] = []