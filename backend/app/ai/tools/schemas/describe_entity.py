from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field


class DescribeEntityInput(BaseModel):
    """Input for describing a catalog entity (model/metric).

    - name_or_id: Entity ID (UUID), title, or slug to look up
    - should_create: If True, create a tracked step/visualization from the entity
    - should_rerun: If True, re-execute the entity's code instead of using cached data
    """

    name_or_id: str = Field(
        ...,
        description="Entity identifier: UUID, title, or slug",
    )
    should_create: bool = Field(
        default=False,
        description="If True, create a new step and visualization from the entity",
    )
    should_rerun: bool = Field(
        default=False,
        description="If True, re-execute the entity's code to get fresh data",
    )


class DescribeEntityOutput(BaseModel):
    """Output from describe_entity tool."""

    success: bool = Field(..., description="Whether the operation succeeded")
    
    # Entity metadata
    entity_id: Optional[str] = Field(default=None, description="Entity UUID")
    entity_type: Optional[str] = Field(default=None, description="Entity type: model or metric")
    title: Optional[str] = Field(default=None, description="Entity title")
    description: Optional[str] = Field(default=None, description="Entity description")
    code: Optional[str] = Field(default=None, description="Entity SQL/code")
    
    # Data profile (when not creating)
    data_profile: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Data profile with row_count, column_count, columns stats, and optional sample rows",
    )
    
    # Created artifact info (when should_create=True)
    step_id: Optional[str] = Field(default=None, description="Created step ID if should_create=True")
    data_model: Optional[Dict[str, Any]] = Field(default=None, description="Visualization data model")
    view: Optional[Dict[str, Any]] = Field(default=None, description="View schema for rendering")
    
    # Execution info
    execution_log: Optional[str] = Field(default=None, description="Execution log if code was run")
    errors: List[str] = Field(default_factory=list, description="Errors encountered")

