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
    params: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Parameter VALUES for a parameterized entity ({name: value}), using the "
            "names listed under the entity's <parameters> in <entities>. The saved "
            "code runs with these values — no code generation. Omit a parameter to "
            "use its default; omit `params` entirely for an entity without "
            "parameters. Identity-source parameters are resolved from the viewer "
            "server-side and must not be set here."
        ),
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
    # The budgeted preview the planner reads — the same shape create_data and
    # read_query produce (build_data_preview), so history digests, result
    # projections and observation compaction treat all three alike.
    data_preview: Optional[Dict[str, Any]] = Field(default=None, description="Budgeted, self-describing preview of the entity rows")
    stats: Optional[Dict[str, Any]] = Field(default=None, description="Per-column stats of the entity rows")
    
    # Created artifact info (when should_create=True)
    step_id: Optional[str] = Field(default=None, description="Created step ID if should_create=True")
    data_model: Optional[Dict[str, Any]] = Field(default=None, description="Visualization data model")
    view: Optional[Dict[str, Any]] = Field(default=None, description="View schema for rendering")
    
    # Declared parameters (ParamSpec dicts) and the values this result was
    # produced with — persisted onto the created query/step so the widget
    # keeps its parameter controls and viewer runs re-bind them.
    parameters: Optional[List[Dict[str, Any]]] = Field(default=None, description="Declared parameters of the entity")
    applied_params: Optional[Dict[str, Any]] = Field(default=None, description="Resolved parameter values this data was produced with")

    # Execution info
    execution_log: Optional[str] = Field(default=None, description="Execution log if code was run")
    errors: List[str] = Field(default_factory=list, description="Errors encountered")

