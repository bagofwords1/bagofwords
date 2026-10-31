"""Shared application registration and access settings."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    agent_ids: Optional[list[str]] = Field(default_factory=list, max_length=500)

    @field_validator('name')
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError('Name is required')
        return value.strip()


class ApplicationUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    enabled: Optional[bool] = None
    agent_ids: Optional[list[str]] = Field(default=None, max_length=500)

    @field_validator('name', 'enabled')
    @classmethod
    def not_null(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError('Value is required')
        return value.strip() if isinstance(value, str) else value
