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
    edit_prompt: str = Field(..., description="Natural language description of the change to make. Be specific about what to change and how. E.g., 'Remove the filter bar', 'Make the revenue chart blue', 'Add a KPI card for total users'. Also use this to fix visual issues from a previous create_artifact (e.g., 'the bar chart is cut off on the right side', 'KPI cards are overlapping'). If adding new visualizations, describe where they should go in the layout.")
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
