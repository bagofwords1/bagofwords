from sqlalchemy import Column, String, ForeignKey, Integer, JSON, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class Artifact(BaseSchema):
    """
    Stores AI-generated artifacts (React code) for dashboards.

    Supports two modes:
    - 'page': Single-page artifact with full React code
    - 'slides': Multi-slide presentation (future support)

    The content column is flexible JSON that varies by mode:
    - page mode: { "code": "<React JSX code>" }
    - slides mode: { "slides": [{ "code": "...", "title": "..." }, ...] }
    """
    __tablename__ = 'artifacts'

    # The report this artifact belongs to
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    report = relationship("Report", back_populates="artifacts", lazy="selectin")

    # User who created this artifact
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    user = relationship("User", lazy="selectin")

    # Organization for multi-tenancy
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    organization = relationship("Organization", lazy="selectin")

    # Artifact metadata
    title = Column(String(255), nullable=True, default="Untitled Artifact")

    # Mode: 'page' or 'slides'
    mode = Column(String(20), nullable=False, default='page', index=True)

    # Version number for tracking iterations
    version = Column(Integer, nullable=False, default=1)

    # Flexible content storage - structure depends on mode
    # For 'page': { "code": "<full React JSX>" }
    # For 'slides': { "slides": [{ "code": "...", "title": "...", "order": 0 }, ...] }
    content = Column(JSON, nullable=False, default=dict)

    # Optional: Store the prompt that generated this artifact
    generation_prompt = Column(Text, nullable=True)

    # Status: 'pending', 'completed', 'failed'
    status = Column(String(20), nullable=False, default='completed', index=True)

    # Thumbnail path for preview cards (relative to uploads folder)
    thumbnail_path = Column(String(512), nullable=True)

    # Optional: Reference to the completion that generated this
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=True, index=True)
    completion = relationship("Completion", lazy="selectin")
