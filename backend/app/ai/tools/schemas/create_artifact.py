from typing import Optional, Literal, Dict, Any
from pydantic import BaseModel, Field


class CreateArtifactInput(BaseModel):
    """Input for create_artifact tool.

    - prompt: user's goal/instruction for the artifact
    - title: optional title for the artifact
    - mode: 'page' for single-page dashboard artifact 
    - style_hints: optional styling preferences (e.g., "dark theme", "minimal")
    """

    prompt: str = Field(..., description="User's instruction for what the artifact should display/visualize")
    title: Optional[str] = Field(None, description="Title for the artifact")
    mode: Literal["page"] = Field(default="page", description="Artifact mode: page (dashboard)")
    style_hints: Optional[str] = Field(None, description="Optional styling preferences")


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
