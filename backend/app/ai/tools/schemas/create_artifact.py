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
        "Structured build plan for the dashboard. This prompt drives the entire code generation — be specific and use these sections:\n\n"
        "## Layout\n"
        "Overall structure and viz placement. For each viz: title, chart type, position, local filter yes/no.\n"
        "Example: 'KPI row at top from Dashboard KPIs. Below: 2-col grid — Sales Trend as line chart left, Top Artists as horizontal bar right.'\n\n"
        "## Filters\n"
        "Global filters if 2+ vizs share a filterable column. Which columns, which vizs, column name mappings if names differ. Omit if none.\n"
        "Example: 'Global year filter across all 3 vizs. Column is `year` in all.'\n\n"
        "## Theme\n"
        "Colors, dark/light, spacing, typography, design feel. Capture the user's style request verbatim — this section overrides all system defaults.\n"
        "Example: 'Flat BI style — white bg, no shadows, no gradients, subtle borders, tight spacing, neutral typography. NOT executive/marketing.'\n\n"
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
