from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class UsagePolicyAssignmentInput(BaseModel):
    principal_type: str
    principal_id: str

    @field_validator("principal_type")
    @classmethod
    def valid_principal_type(cls, value: str) -> str:
        if value not in {"user", "group", "role"}:
            raise ValueError("principal_type must be user, group, or role")
        return value


class UsagePolicyConnectionOverrideInput(BaseModel):
    connection_id: str
    monthly_query_limit: Optional[int] = Field(default=None, ge=0)
    monthly_data_bytes_limit: Optional[int] = Field(default=None, ge=0)


class UsagePolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    monthly_token_limit: Optional[int] = Field(default=None, ge=0)
    monthly_query_limit: Optional[int] = Field(default=None, ge=0)
    monthly_data_bytes_limit: Optional[int] = Field(default=None, ge=0)
    enabled: bool = True
    assignments: List[UsagePolicyAssignmentInput] = []
    connection_overrides: List[UsagePolicyConnectionOverrideInput] = []

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Policy name cannot be empty")
        return value


class UsagePolicyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    monthly_token_limit: Optional[int] = Field(default=None, ge=0)
    monthly_query_limit: Optional[int] = Field(default=None, ge=0)
    monthly_data_bytes_limit: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None
    assignments: Optional[List[UsagePolicyAssignmentInput]] = None
    connection_overrides: Optional[List[UsagePolicyConnectionOverrideInput]] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Policy name cannot be empty")
        return value


class UsagePolicyAssignmentSchema(UsagePolicyAssignmentInput):
    id: str
    policy_id: str
    organization_id: str

    class Config:
        from_attributes = True


class UsagePolicyPrincipalAssignmentUpdate(UsagePolicyAssignmentInput):
    policy_id: Optional[str] = None


class UsagePolicyPrincipalAssignmentResult(BaseModel):
    principal_type: str
    principal_id: str
    policy_id: Optional[str] = None
    assignment: Optional[UsagePolicyAssignmentSchema] = None


class UsagePolicyConnectionOverrideSchema(UsagePolicyConnectionOverrideInput):
    id: str
    policy_id: str
    organization_id: str

    class Config:
        from_attributes = True


class UsagePolicySchema(BaseModel):
    id: str
    organization_id: str
    name: str
    description: Optional[str] = None
    monthly_token_limit: Optional[int] = None
    monthly_query_limit: Optional[int] = None
    monthly_data_bytes_limit: Optional[int] = None
    enabled: bool
    assignments: List[UsagePolicyAssignmentSchema] = []
    connection_overrides: List[UsagePolicyConnectionOverrideSchema] = []

    class Config:
        from_attributes = True


class EffectiveUsagePolicySchema(BaseModel):
    enabled: bool
    organization_id: str
    user_id: str
    monthly_token_limit: Optional[int] = None
    monthly_query_limit: Optional[int] = None
    monthly_data_bytes_limit: Optional[int] = None
    policy_ids: List[str] = []
    resolution_source: str = "default"
