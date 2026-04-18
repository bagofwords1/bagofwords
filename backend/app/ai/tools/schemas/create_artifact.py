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
        "Overall structure and viz placement. For each viz in `visualization_ids`: title, chart type, position, local filter yes/no. "
        "If a `current_artifact` exists, describe how existing vizs remain on the canvas AND where new additions go — do not describe a layout that silently drops prior vizs.\n"
        "Example: 'KPI row at top from Dashboard KPIs. Below: 2-col grid — Sales Trend as line chart left, Top Artists as horizontal bar right.'\n\n"
        "## Filters\n"
        "Global filters if 2+ vizs share a filterable column. Which columns, which vizs, column name mappings if names differ. Omit if none.\n"
        "Example: 'Global year filter across all 3 vizs. Column is `year` in all.'\n\n"
        "## Theme\n"
        "Colors, dark/light, spacing, typography, design feel. Capture the user's style request verbatim — this section overrides all system defaults.\n"
        "Example: 'Flat BI style — white bg, no shadows, no gradients, subtle borders, tight spacing, neutral typography. NOT executive/marketing.'\n\n"
        "CONTRACT: If your prompt specifies a cross-viz behavior (global filter, time comparison, slice/groupby, rank-across, drill-down), you MUST have already completed the Dashboard Contract preflight (see planner instructions): every viz_id in `visualization_ids` satisfies that contract directly, or you rebuilt its data via `create_data` this turn, or it was dropped/substituted because it was meaningless under the contract. Do NOT include a viz that can't participate in the declared contract — that ships a dashboard where the filter works on some charts and not others.\n\n"
        "CONTINUITY: When a `current_artifact` exists and the user is asking to improve/enhance/rework it, your prompt describes a CHANGE to that artifact, not a fresh build. "
        "Preserve the existing title (don't invent 'Enhanced X' / 'Improved Y') unless the user asked to rename. Describe ALL existing vizs in the layout (they're still on the canvas) plus the new additions. "
        "Prefer `edit_artifact` for small/additive changes; only use `create_artifact` when the change is structurally too large for surgical diffs — and even then, carry all prior viz_ids forward.\n\n"
        "Do NOT use this tool to modify an existing artifact; use edit_artifact instead."
    ))
    title: Optional[str] = Field(None, description="Title for the artifact, make it concise and descriptive for end users")
    mode: Literal["page", "slides"] = Field(default="page", description="Artifact mode: 'page' for dashboards or 'slides' for presentations")
    visualization_ids: List[str] = Field(..., min_length=1, description=(
        "Ordered list of visualization IDs (UUIDs) to include. Find these in previous create_data results as 'viz_id: <uuid>'. Must contain at least one. "
        "CONTINUITY: When a `current_artifact` exists in context, this list MUST be a superset of its existing viz_ids — carry forward every viz unless the user explicitly asked to remove one. "
        "Phrases like 'improve', 'add KPIs', 'make it amazing', 'redesign', 'add a chart' are ADDITIVE — they never imply removal. "
        "Drop a viz only on explicit instruction ('remove the customers chart', 'get rid of the KPI row') OR when the Dashboard Contract preflight classified it as meaningless under the contract (e.g., `Total Customers` under a customer filter = 1). "
        "Every viz in this list must be able to participate in any cross-viz contract your prompt declares (filter/compare/slice/rank/drill) — if it can't, rebuild its data via `create_data` first and swap in the new viz_id, or drop it."
    ))


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
