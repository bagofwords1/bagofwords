import uuid
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from fastapi_users import schemas
from datetime import datetime
from app.schemas.external_user_mapping_schema import ExternalUserMappingMinimalSchema


# class UserBase(BaseModel):
    # email: EmailStr
    # username: str = Field(..., min_length=3, max_length=50)

class UserCreate(schemas.BaseUserCreate):
    # pass
    name: str = Field(..., min_length=3, max_length=50)
    # Optional invite token from the sign-up link. Used only to validate the
    # invite at registration; never persisted on the User row.
    invite_token: Optional[str] = None
    #password: str = Field(..., min_length=6)

    def create_update_dict(self):
        d = super().create_update_dict()
        d.pop("invite_token", None)
        return d

class UserUpdate(schemas.BaseUserUpdate):
    # Allow users to edit their display name from the profile modal. fastapi-users
    # applies this through the standard PATCH /users/me self-update flow.
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    #password: Optional[str] = Field(None, min_length=6)

class UserRead(schemas.BaseUser[uuid.UUID]):
    name: str

    # class Config:
      #  orm_mode = True  # Allows the output model to be compatible with ORM objects

class UserSchema(BaseModel):
    id: str
    name: str
    email: str
    external_user_mappings: List[ExternalUserMappingMinimalSchema] = []
    last_login: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    class Config:
        from_attributes = True