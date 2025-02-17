from pydantic import BaseModel
from uuid import UUID
from typing import List, Optional
from app.schemas.user_schema import UserSchema

class OrganizationCreate(BaseModel):
    name: str
    description: Optional[str] = None

class OrganizationSchema(OrganizationCreate):
    id: str

    class Config:
        from_attributes = True

class MembershipCreate(BaseModel):
    organization_id: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = "member"

class MembershipUpdate(BaseModel):
    role: Optional[str] = None


class MembershipSchema(MembershipCreate):
    id: str
    user: Optional[UserSchema] = None
    email: Optional[str] = None

    class Config:
        from_attributes = True

class OrganizationAndRoleSchema(OrganizationSchema):
    role: str