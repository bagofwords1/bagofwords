from pydantic import BaseModel, Field, model_validator
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


class ArtifactContentDoc(BaseModel):
    """Content structure for doc mode artifacts (markdown documents)."""
    markdown: str
    visualization_ids: List[str] = Field(default_factory=list)


class ArtifactBase(BaseModel):
    """Base schema for Artifact."""
    title: Optional[str] = "Untitled Artifact"
    mode: Literal["page", "slides", "doc"] = "page"


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
    # Which of content.visualization_ids the page code actually renders.
    # Membership in visualization_ids only means "attached to the dashboard";
    # a viz whose id the code never binds silently disappears from the page —
    # this is how clients tell the two states apart.
    used_visualization_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _compute_used_visualization_ids(self):
        content = self.content or {}
        viz_ids = [str(v) for v in (content.get("visualization_ids") or [])]
        code = content.get("code")
        if code is None:
            # doc/slides content has no page code to scan — every member counts
            self.used_visualization_ids = viz_ids
        else:
            # Lazy: importing the tools package at module load would drag in
            # every tool implementation (and risk cycles)
            from app.ai.tools.implementations._artifact_refs import referenced_viz_ids
            self.used_visualization_ids = referenced_viz_ids(code, viz_ids)
        return self

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


