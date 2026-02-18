from typing import Optional, List
from pydantic import BaseModel, Field


class ReadArtifactInput(BaseModel):
    """Input for read_artifact tool.

    - artifact_id: ID of the artifact to read (from previous create_artifact results)
    """

    artifact_id: str = Field(
        ...,
        description="ID of the artifact to read. Find this in previous create_artifact results as 'artifact_id: <uuid>' in the conversation history."
    )


class ReadArtifactOutput(BaseModel):
    """Output from read_artifact tool.

    Returns the artifact's code and metadata for iteration/refinement.
    """

    artifact_id: str = Field(..., description="ID of the artifact")
    title: Optional[str] = Field(None, description="Artifact title")
    mode: str = Field(..., description="Artifact mode: 'page' or 'slides'")
    code: str = Field(..., description="The React/JSX code of the artifact")
    visualization_ids: List[str] = Field(default_factory=list, description="Visualization IDs used in this artifact")
    version: int = Field(default=1, description="Current version number")
