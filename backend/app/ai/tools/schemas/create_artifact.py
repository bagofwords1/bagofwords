from typing import Optional, Literal, Dict, Any, List
from pydantic import BaseModel, Field


class CreateArtifactInput(BaseModel):
    """Input for create_artifact tool.

    - prompt: user's goal/instruction for the artifact (can include style preferences)
    - title: optional title for the artifact
    - mode: 'page' for dashboards, 'slides' for presentations
    - visualization_ids: ordered list of visualization IDs to include
    """

    prompt: str = Field(..., description="Instructions for the artifact. For new artifacts: describe layout, visualizations, and style. For modifications: describe what to change - previous code is automatically available in context.")
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
    version: int = Field(default=1, description="Version number of the artifact")
