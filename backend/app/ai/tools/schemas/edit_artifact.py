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
    edit_instruction: str = Field(..., description="Natural language description of the change to make. E.g., 'Remove the filter bar', 'Make the revenue chart blue', 'Add a KPI card for total users'.")
    visualization_ids: Optional[List[str]] = Field(default=None, description="Optional list of NEW visualization IDs to add to the artifact. Existing visualization IDs are kept automatically. Only provide this if the edit requires adding new visualizations.")
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
    version: int = Field(..., description="Bumped version number of the artifact")
    diff_applied: bool = Field(..., description="True if the edit was applied as a surgical search/replace diff. False if the tool fell back to a full code rewrite.")
