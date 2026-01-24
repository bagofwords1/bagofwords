from typing import Optional, Literal, Dict, Any, List
from pydantic import BaseModel, Field


class CreateArtifactInput(BaseModel):
    """Input for create_artifact tool.

    - prompt: user's goal/instruction for the artifact (can include style preferences)
    - title: optional title for the artifact
    - mode: 'page' for single-page dashboard artifact
    - visualization_ids: ordered list of visualization IDs to include
    """

    prompt: str = Field(..., description="User's goal for what the artifact should display/visualize, including any style preferences")
    title: Optional[str] = Field(None, description="Title for the artifact, make it concise and descriptive for end users")
    mode: Literal["page"] = Field(default="page", description="Artifact mode: page (dashboard)")
    visualization_ids: List[str] = Field(..., description="Ordered list of visualization IDs to include in the artifact. Be sure to include only visualizations that are important to dashboard goal and narrative.")


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
    version: int = Field(default=1, description="Version number of the artifact")
