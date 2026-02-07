from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime


class SlideContent(BaseModel):
    """Content for a single slide in slides mode."""
    code: str
    title: Optional[str] = None
    order: int = 0


class ArtifactContentPage(BaseModel):
    """Content structure for page mode artifacts."""
    code: str


class ArtifactContentSlides(BaseModel):
    """Content structure for slides mode artifacts."""
    slides: List[SlideContent]


class ArtifactBase(BaseModel):
    """Base schema for Artifact."""
    title: Optional[str] = "Untitled Artifact"
    mode: Literal["page", "slides"] = "page"


class ArtifactCreate(ArtifactBase):
    """Schema for creating a new artifact."""
    report_id: str
    content: dict  # Either ArtifactContentPage or ArtifactContentSlides
    generation_prompt: Optional[str] = None
    completion_id: Optional[str] = None


class ArtifactUpdate(BaseModel):
    """Schema for updating an existing artifact."""
    title: Optional[str] = None
    content: Optional[dict] = None
    generation_prompt: Optional[str] = None


class ArtifactSchema(ArtifactBase):
    """Full artifact schema for API responses."""
    id: str
    report_id: str
    user_id: str
    organization_id: str
    version: int
    content: dict
    generation_prompt: Optional[str] = None
    completion_id: Optional[str] = None
    status: str = "completed"
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ArtifactListSchema(BaseModel):
    """Schema for listing artifacts (lighter weight)."""
    id: str
    report_id: str
    title: Optional[str]
    mode: str
    version: int
    status: str = "completed"
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ArtifactRecentSchema(BaseModel):
    """Schema for recent artifacts with thumbnail and report info."""
    id: str
    report_id: str
    user_id: str
    title: Optional[str]
    mode: str
    version: int
    status: str = "completed"
    thumbnail_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    report_title: Optional[str] = None
    is_published: bool = False
    user_name: Optional[str] = None

    class Config:
        from_attributes = True

    @classmethod
    def from_artifact(cls, artifact) -> "ArtifactRecentSchema":
        """Create schema from an Artifact model with report relationship loaded."""
        user_name = None
        if artifact.user:
            user_name = artifact.user.name or artifact.user.email

        return cls(
            id=str(artifact.id),
            report_id=str(artifact.report_id),
            user_id=str(artifact.user_id),
            title=artifact.title,
            mode=artifact.mode,
            version=artifact.version,
            status=artifact.status,
            thumbnail_path=artifact.thumbnail_path,
            created_at=artifact.created_at,
            updated_at=artifact.updated_at,
            report_title=artifact.report.title if artifact.report else None,
            is_published=artifact.report.status == "published" if artifact.report else False,
            user_name=user_name,
        )
