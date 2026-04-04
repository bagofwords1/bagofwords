from typing import Optional, Literal, Dict, Any, List
from pydantic import BaseModel, Field


class CreateArtifactInput(BaseModel):
    """Input for create_artifact tool.

    - prompt: user's goal/instruction for the artifact (can include style preferences)
    - title: optional title for the artifact
    - mode: 'page' for dashboards, 'slides' for presentations
    - visualization_ids: ordered list of visualization IDs to include
    """

    prompt: str = Field(..., description=(
        "Detailed build plan for the dashboard. This prompt drives the entire code generation — be specific and structured. "
        "Include:\n"
        "1. LAYOUT & VISUALIZATIONS: Overall structure (e.g. KPI row at top, 2-col grid, full-width section). "
        "For each visualization, specify: which viz (by title), chart type to render it as, "
        "position in the layout, and whether it should have a local filter (yes/no).\n"
        "2. GLOBAL FILTERS (if applicable): If 2+ visualizations share a filterable column "
        "(same column name or same concept under different names), define a global filter bar. "
        "Specify which columns to filter on and which vizs they affect. "
        "If column names differ across vizs, note the mapping.\n"
        "3. THEME & STYLE: Color scheme, dark/light mode, any design preferences.\n"
        "Do NOT use this tool to modify an existing artifact; use edit_artifact instead."
    ))
    title: Optional[str] = Field(None, description="Title for the artifact, make it concise and descriptive for end users")
    mode: Literal["page", "slides"] = Field(default="page", description="Artifact mode: 'page' for dashboards or 'slides' for presentations")
    visualization_ids: List[str] = Field(..., min_length=1, description="Ordered list of visualization IDs (UUIDs) to include. Find these in previous create_data results as 'viz_id: <uuid>'. Must contain at least one. Include only visualizations important to the dashboard goal.")


class CreateArtifactOutput(BaseModel):
    """Output from create_artifact tool.

    - artifact_id: ID of the created artifact
    - code: the generated React/JSX code
    - mode: the artifact mode (page/slides)
    - title: the artifact title
    """

    artifact_id: str = Field(..., description="ID of the created artifact in the database")
    code: str = Field(..., description="The generated React/JSX code")
    mode: str = Field(..., description="Artifact mode, eiither 'page' for dashboards/reports or 'slides' for presentation, deck or powerpoint export")
    title: Optional[str] = Field(None, description="Artifact title")
    visualization_ids: List[str] = Field(default_factory=list, description="All visualization IDs included in this artifact. Use these when making further edits with edit_artifact.")
    version: int = Field(default=1, description="Version number of the artifact")
