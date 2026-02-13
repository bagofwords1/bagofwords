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
    # enabled: bool = True  # Keep for backward compatibility - REMOVED
    value: Optional[Any] = None
    name: str
    description: str
    is_lab: bool = False
    editable: bool = True
    state: FeatureState = FeatureState.ENABLED # Default state

    @validator('value', pre=True, always=True)
    def set_default_value_if_none(cls, v, values):
        """Set default value based on state if value is None"""
        if v is None:
            # Default value to True if state is ENABLED, False otherwise
            return values.get('state', FeatureState.ENABLED) == FeatureState.ENABLED
        return v

    @validator('state', pre=True, always=True)
    def set_state_from_value(cls, v, values):
        """Set state based on value field if state is not provided or applicable"""
        # If state is already set (e.g., to LOCKED), respect it.
        if v is not None and v != FeatureState.ENABLED and v != FeatureState.DISABLED:
            return v

        # Determine state from value if value is boolean
        value = values.get('value')
        if isinstance(value, bool):
            return FeatureState.ENABLED if value else FeatureState.DISABLED
        # Fallback to ENABLED if value isn't boolean and state isn't set
        return v or FeatureState.ENABLED


    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        """Ensure state reflects value unless explicitly different (e.g., LOCKED)"""
        d = super().dict(*args, **kwargs)
        # Ensure state is consistent with boolean value if not LOCKED
        if isinstance(self.value, bool) and self.state != FeatureState.LOCKED:
             d['state'] = FeatureState.ENABLED if self.value else FeatureState.DISABLED
        # Ensure value is consistent with state if value is boolean
        if isinstance(self.value, bool):
             d['value'] = (self.state == FeatureState.ENABLED)
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

    # @validator('value') # Keep this if specific validation rules are needed later
    # def validate_value(cls, v, values):
    #     """Validate that value is appropriate for the feature."""
    #     # Add any specific validation rules here
    #     return v

class OrganizationSettingsConfig(BaseModel):
    # General (workspace) settings
    class GeneralConfig(BaseModel):
        ai_analyst_name: str = "AI Analyst"
        bow_credit: bool = True
        # Icon storage fields (disk/object storage)
        icon_key: Optional[str] = None
        icon_url: Optional[str] = None

        @validator('ai_analyst_name')
        def validate_ai_name(cls, v: str) -> str:
            name = (v or "").strip()
            if len(name) == 0:
                raise ValueError("AI analyst name cannot be empty")
            if len(name) > 50:
                raise ValueError("AI analyst name must be 50 characters or less")
            return name

    general: GeneralConfig = GeneralConfig()

    # Update defaults to use 'value' instead of 'enabled'
    allow_llm_see_data: FeatureConfig = FeatureConfig(value=True, name="Allow LLM to see data", description="Enable LLM to see data as part of the analysis and user queries", is_lab=False, editable=True)
    enable_training_mode: FeatureConfig = FeatureConfig(value=True, name="Training Mode", description="Enable training mode for admins to work with the agent to build documentation, instructions, semantics and guidlines ", is_lab=False, editable=True)
    enable_file_upload: FeatureConfig = FeatureConfig(value=True, name="Allow file upload", description="Allow users to upload spreadsheets and documents (xls/pdf) and push their content to the LLM", is_lab=False, editable=True)
    enable_code_editing: FeatureConfig = FeatureConfig(value=True, name="Allow users to edit and execute the LLM generated code", description="Allow users to edit and execute the LLM generated code", is_lab=False, editable=True)
    enable_llm_judgement: FeatureConfig = FeatureConfig(value=True, name="Enable LLM Judge", description="Enable LLM to judge the quality of the analysis and user queries", is_lab=False, editable=True)
    suggest_instructions: FeatureConfig = FeatureConfig(value=True, name="Autogenerate instructions", description="Automatically generate instructions following clarifications provided by the user", is_lab=False, editable=True)
    # validate_code: FeatureConfig = FeatureConfig(value=True, name="Validate code", description="Validate the code generated by the LLM", is_lab=False, editable=True)
    limit_row_count: FeatureConfig = FeatureConfig(value=1000, name="Limit row count", description="Limit the number of rows that can be displayed in tables and data previews", is_lab=False, editable=True)
    limit_analysis_steps: FeatureConfig = FeatureConfig(value=6, name="Limit analysis steps", description="Limit the number of analysis steps that can be used in the analysis", is_lab=False, editable=False) # Assuming value is int here
    limit_code_retries: FeatureConfig = FeatureConfig(value=3, name="Limit code retries", description="Limit the number of times the LLM can retry code generation", is_lab=False, editable=False) # Assuming value is int here
    top_k_schema: FeatureConfig = FeatureConfig(value=10, name="Top K schema", description="The number of schema to sample from the data source in the Agent", is_lab=False, editable=True) # Assuming value is int here
    top_k_metadata_resources: FeatureConfig = FeatureConfig(value=10, name="Top K metadata resources", description="The number of metadata resources to sample from the data source in the Agent", is_lab=False, editable=True) # Assuming value is int here
    mcp_enabled: FeatureConfig = FeatureConfig(value=True, name="MCP", description="Enable Model Context Protocol (MCP) endpoint for integration with AI assistants like Cursor, Claude, or others", is_lab=False, editable=True)
    max_instructions_in_context: FeatureConfig = FeatureConfig(value=50, name="Max instructions in context", description="Maximum number of instructions to include in AI context. 'Always' instructions are loaded first, then 'intelligent' instructions fill remaining slots.", is_lab=False, editable=True)
    step_retention_days: FeatureConfig = FeatureConfig(value=14, name="Widget Data Retention Days", description="Number of days to retain widgets data before purging.", is_lab=False, editable=True)

    ai_features: Dict[str, FeatureConfig] = {
        # Update defaults to use 'value' instead of 'enabled'
        "planner": FeatureConfig(value=True, name="Planner", description="Orchestrates analysis by breaking down user requests into actionable steps", is_lab=False, editable=False),
        "coder": FeatureConfig(value=True, name="Coder", description="Translates data models into executable Python code for data processing", is_lab=False, editable=False),
        "validator": FeatureConfig(value=True, name="Validator", description="Validates code safety and integrity and its data model compatibility", is_lab=False, editable=True),
        "dashboard_designer": FeatureConfig(value=True, name="Dashboard Designer", description="Creates layout and organization of dashboard elements", is_lab=False),
        "analyze_data": FeatureConfig(value=False, name="Analyze Data", description="Provides natural language responses to user questions about their data", is_lab=False, editable=False),
        "code_reviewer": FeatureConfig(value=False, name="Code Reviewer", description="Allow users to get feedback on their code", is_lab=False), # Changed enabled=True to value=False based on previous value
        "search_context": FeatureConfig(value=True, name="Search Context", description="Allow users to search through metadata, context, and data models", is_lab=False),
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