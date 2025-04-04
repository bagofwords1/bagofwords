from pydantic import BaseModel, validator, Field
from typing import Dict, Any, Optional, Union, List
import json
from datetime import datetime
from enum import Enum

class FeatureState(str, Enum):
    """Explicit states for features"""
    ENABLED = "enabled"
    DISABLED = "disabled"
    LOCKED = "locked"

class FeatureConfig(BaseModel):
    enabled: bool = True  # Keep for backward compatibility
    value: Optional[Any] = None
    name: str
    description: str
    is_lab: bool = False
    editable: bool = True
    state: FeatureState = FeatureState.ENABLED

    @validator('state', pre=True)
    def set_state_from_enabled(cls, v, values):
        """Set state based on enabled field if state is not provided"""
        if v is None and 'enabled' in values:
            return FeatureState.ENABLED if values['enabled'] else FeatureState.DISABLED
        return v

    @validator('enabled', pre=True)
    def set_enabled_from_state(cls, v, values):
        """Set enabled based on state if enabled is not provided"""
        if v is None and 'state' in values:
            return values['state'] == FeatureState.ENABLED
        return v

    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        """Ensure both state and enabled are included in dict output"""
        d = super().dict(*args, **kwargs)
        d['enabled'] = self.enabled  # Ensure enabled is always set based on state
        return d

    class Config:
        validate_assignment = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeatureConfig":
        """Create a FeatureConfig from a dictionary, with proper defaults."""
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return self.model_dump()

    def merge(self, other: Union[Dict[str, Any], "FeatureConfig"]) -> "FeatureConfig":
        """Merge with another FeatureConfig or dict, preserving existing values."""
        if isinstance(other, dict):
            other_dict = other
        else:
            other_dict = other.to_dict()
        
        current = self.to_dict()
        current.update(other_dict)
        return FeatureConfig(**current)

    @validator('value')
    def validate_value(cls, v, values):
        """Validate that value is appropriate for the feature."""
        # Add any specific validation rules here
        return v

class OrganizationSettingsConfig(BaseModel):
    allow_llm_see_data: FeatureConfig = FeatureConfig(enabled=False, name="Allow LLM to see data", description="Enable LLM to see data as part of the analysis and user queries", is_lab=False, editable=True)
    allow_file_upload: FeatureConfig = FeatureConfig(enabled=True, name="Allow file upload", description="Allow users to upload spreadsheets and docuemnts (xls/pdf) and push their content to the LLM", is_lab=False, editable=False)
    allow_code_editing: FeatureConfig = FeatureConfig(enabled=False, name="Allow users to edit and execute the LLM generated code", description="Allow users to edit and execute the LLM generated code", is_lab=False, editable=False)
    #limit_row_count: FeatureConfig = FeatureConfig(enabled=True, value=1000, name="Limit row count", description="Limit the number of rows that can be showed in the table or stored in the database cache", is_lab=False, editable=False)
    limit_analysis_steps: FeatureConfig = FeatureConfig(enabled=True, value=5, name="Limit analysis steps", description="Limit the number of analysis steps that can be used in the analysis", is_lab=False, editable=False)
    limit_code_retries: FeatureConfig = FeatureConfig(enabled=True, value=3, name="Limit code retries", description="Limit the number of times the LLM can retry code generation", is_lab=False, editable=False)

    ai_features: Dict[str, FeatureConfig] = {
        "planner": FeatureConfig(enabled=True, name="Planner", description="Orchestrates analysis by breaking down user requests into actionable steps", is_lab=False, editable=False),
        "coder": FeatureConfig(enabled=True, name="Coder", description="Translates data models into executable Python code for data processing", is_lab=False, editable=False),
        "validator": FeatureConfig(enabled=True, name="Validator", description="Validates code safety and integrity and its data model compatibility", is_lab=False, editable=True),
        "dashboard_designer": FeatureConfig(enabled=True, name="Dashboard Designer", description="Creates layout and organization of dashboard elements", is_lab=False),
        "analyze_data": FeatureConfig(enabled=False, name="Analyze Data", description="Provides natural language responses to user questions about their data", is_lab=False, editable=False),
        "code_reviewer": FeatureConfig(enabled=True, name="Code Reviewer", description="Allow users to get feedback on their code", is_lab=False),
        "search_context": FeatureConfig(enabled=True, name="Search Context", description="Allow users to search through metadata, context, and data models", is_lab=False),
    }


class OrganizationSettingsBase(BaseModel):
    organization_id: str
    config: OrganizationSettingsConfig

class OrganizationSettingsCreate(OrganizationSettingsBase):
    pass

class OrganizationSettingsUpdate(BaseModel):
    config: Optional[Dict[str, Any]] = None

class OrganizationSettingsSchema(OrganizationSettingsBase):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True 