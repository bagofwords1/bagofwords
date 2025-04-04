from pydantic import BaseModel, validator, Field
from typing import Dict, Any, Optional, Union, List
import json
from datetime import datetime

class FeatureConfig(BaseModel):
    enabled: bool = True
    value: Optional[Any] = None
    name: str
    description: str
    is_lab: bool = False
    editable: bool = True

class OrganizationSettingsConfig(BaseModel):
    allow_llm_see_data: FeatureConfig = FeatureConfig(enabled=False, name="Allow LLM to see data", description="Enable LLM to see data as part of the analysis and user queries", is_lab=False, editable=True)
    allow_file_upload: FeatureConfig = FeatureConfig(enabled=True, name="Allow file upload", description="Allow users to upload spreadsheets and docuemnts (xls/pdf) and push their content to the LLM", is_lab=False, editable=False)
    allow_code_editing: FeatureConfig = FeatureConfig(enabled=False, name="Allow users to edit and execute the LLM generated code", description="Allow users to edit and execute the LLM generated code", is_lab=False, editable=False)
    #limit_row_count: FeatureConfig = FeatureConfig(enabled=True, value=1000, name="Limit row count", description="Limit the number of rows that can be showed in the table or stored in the database cache", is_lab=False, editable=False)

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