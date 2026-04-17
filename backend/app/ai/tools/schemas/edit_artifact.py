from typing import Optional, List
from pydantic import BaseModel, Field


class EditArtifactInput(BaseModel):
    """Input for edit_artifact tool.

    - artifact_id: ID of the existing artifact to edit (from previous create_artifact/read_artifact)
    - edit_instruction: natural language description of the change to make
    - visualization_ids: optional list of NEW visualization IDs to add (existing ones are kept automatically)
    - title: optional updated title
    """

    artifact_id: str = Field(..., description="ID of the existing artifact to edit. Find this in previous create_artifact or read_artifact results as 'artifact_id: <uuid>' in the conversation.")
    edit_prompt: str = Field(..., description=(
        "Structured edit request. Be specific about what to change. Use relevant sections:\n\n"
        "## Layout changes\n"
        "What to move, add, resize. E.g., 'Move KPI row above the chart grid', 'Add a new bar chart in the bottom-right panel'. "
        "Only describe REMOVAL if the user explicitly asked to remove something — otherwise the edit is additive.\n\n"
        "## Style changes\n"
        "Colors, theme, spacing, typography changes. Capture the user's visual intent verbatim — this overrides existing styling.\n"
        "E.g., 'Switch to dark mode with slate-900 bg', 'Remove all shadows and gradients, flat BI look'.\n\n"
        "## Filter changes\n"
        "Add/remove/fix filter behavior. E.g., 'Add global year filter across all vizs', 'Remove the city local filter'.\n\n"
        "## Data changes\n"
        "Chart type changes, KPI calculations, new data mappings. E.g., 'Change bar chart to horizontal bars', 'Show top 6 artists instead of 10'.\n\n"
        "Only include sections relevant to the edit. Also use this to fix visual issues (e.g., 'the bar chart is cut off', 'KPI cards are overlapping').\n\n"
        "CONTINUITY: Edits are ADDITIVE by default. Phrases like 'improve', 'make it amazing', 'add KPIs', 'redesign' never imply removing existing vizs or content. "
        "Preserve the existing title unless the user asked to rename."
    ))
    visualization_ids: Optional[List[str]] = Field(default=None, description="List of NEW visualization IDs to include in the artifact. IMPORTANT: If you called create_data before this edit, you MUST pass the resulting visualization_id(s) here. Without them, the new visualizations will not appear in the dashboard. Existing visualization IDs from the original artifact are kept automatically — only pass new ones.")
    title: Optional[str] = Field(default=None, description="Updated title for the artifact. If not provided, the existing title is kept.")


class EditArtifactOutput(BaseModel):
    """Output from edit_artifact tool.

    - artifact_id: ID of the edited artifact
    - code: the updated code after applying the edit
    - mode: artifact mode (page/slides)
    - title: the artifact title
    - version: bumped version number
    - diff_applied: whether the edit was applied as a surgical diff (true) or fell back to full rewrite (false)
    """

    artifact_id: str = Field(..., description="ID of the edited artifact")
    code: str = Field(..., description="The updated code after applying the edit")
    mode: str = Field(..., description="Artifact mode: 'page' or 'slides'")
    title: Optional[str] = Field(None, description="Artifact title")
    visualization_ids: List[str] = Field(default_factory=list, description="All visualization IDs included in this artifact after the edit. Use these when making further edits.")
    version: int = Field(..., description="Bumped version number of the artifact")
    diff_applied: bool = Field(..., description="True if the edit was applied as a surgical search/replace diff. False if the tool fell back to a full code rewrite.")
