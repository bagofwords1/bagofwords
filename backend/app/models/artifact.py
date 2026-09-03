from sqlalchemy import Column, String, ForeignKey, Integer, JSON, Text, UniqueConstraint, select
from sqlalchemy.orm import column_property, relationship
from app.models.base import BaseSchema


class Artifact(BaseSchema):
    """One deliverable of a report: a dashboard, a slide deck, or a document.

    This is the IDENTITY row — "the revenue dashboard" — while every saved
    state of it lives in `artifact_versions` (one row per version, related
    via ArtifactVersion.artifact_id). `title` and `mode` live here and only
    here: renaming an artifact renames all its versions at once, and a
    version can never change kind.

    Rows are tiny (five scalar columns), so report-level scans — "does this
    report have artifacts", "which modes" — read this table instead of
    walking version rows.
    """
    __tablename__ = 'artifacts'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    report = relationship("Report", back_populates="artifacts", lazy="selectin")

    # Organization for multi-tenancy
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)

    # User who started this artifact (each version carries its own author)
    created_by = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)

    # Mode: 'page' (dashboard), 'slides' or 'doc' — fixed for life
    mode = Column(String(20), nullable=False, default='page', index=True)

    title = Column(String(255), nullable=True, default="Untitled Artifact")

    # Never loaded implicitly: a version's content JSON can be ~100kB.
    versions = relationship(
        "ArtifactVersion",
        back_populates="artifact",
        lazy="select",
        order_by="ArtifactVersion.version",
    )


class ArtifactVersion(BaseSchema):
    """
    One saved state of an Artifact (AI-generated React code / doc markdown).

    The content column is flexible JSON that varies by the parent's mode:
    - page mode: { "code": "<React JSX code>" }
    - slides mode: { "slides": [{ "code": "...", "title": "..." }, ...] }
    - doc mode: { "markdown": "..." }

    `title` and `mode` are read-through column_properties resolved from the
    parent Artifact — they arrive with every normal SELECT and work in WHERE
    clauses, but three traps follow from that:

    - a `load_only(...)` on this model MUST include `title`, `mode` and
      `artifact_id`, otherwise the first attribute access lazy-loads and
      raises MissingGreenlet under async;
    - an instance that was only flushed needs `await db.refresh()` before
      reading `.title` / `.mode`;
    - assigning `version.title = ...` is a silent no-op — title changes are
      an UPDATE of the parent (artifact_service.new_version(title=...)).

    Construct rows via artifact_service.new_artifact()/new_version(), which
    own the version numbering; the constructor refuses title/mode kwargs.
    """
    __tablename__ = 'artifact_versions'

    # The artifact this row is a version of
    artifact_id = Column(String(36), ForeignKey('artifacts.id'), nullable=False, index=True)
    artifact = relationship("Artifact", back_populates="versions", lazy="select")

    # The report this artifact belongs to. Deliberately duplicated from the
    # parent: the permissions decorator hops version.report_id -> Report,
    # and org scoping filters directly on version rows.
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    report = relationship("Report", back_populates="artifact_versions", lazy="selectin")

    # User who created this version
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    user = relationship("User", lazy="selectin")

    # Organization for multi-tenancy
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    organization = relationship("Organization", lazy="selectin")

    # Version number within the parent artifact (unique per artifact_id)
    version = Column(Integer, nullable=False, default=1)

    # Flexible content storage - structure depends on the parent's mode
    content = Column(JSON, nullable=False, default=dict)

    # Optional: Store the prompt that generated this version
    generation_prompt = Column(Text, nullable=True)

    # Status: 'pending', 'completed', 'failed'
    status = Column(String(20), nullable=False, default='completed', index=True)

    # Thumbnail path for preview cards (relative to uploads folder)
    thumbnail_path = Column(String(512), nullable=True)

    # Path to generated PPTX file (for slides mode)
    pptx_path = Column(String(512), nullable=True)

    # Stored preview screenshot (base64 PNG) from last create/edit render
    screenshot_base64 = Column(Text, nullable=True)

    # JS render errors captured during last screenshot capture
    render_errors = Column(JSON, nullable=True)

    # Optional: Reference to the completion that generated this
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=True, index=True)
    completion = relationship("Completion", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('artifact_id', 'version', name='uq_artifact_versions_artifact_version'),
    )

    # Read-through to the parent (declared after artifact_id on purpose:
    # the correlated subquery binds to the column defined above).
    title = column_property(
        select(Artifact.title)
        .where(Artifact.id == artifact_id)
        .correlate_except(Artifact)
        .scalar_subquery()
    )
    mode = column_property(
        select(Artifact.mode)
        .where(Artifact.id == artifact_id)
        .correlate_except(Artifact)
        .scalar_subquery()
    )

    def __init__(self, **kw):
        # column_property attributes swallow constructor kwargs silently;
        # fail loudly instead — these fields live on the parent Artifact.
        if 'title' in kw or 'mode' in kw:
            raise TypeError(
                "title/mode live on the parent Artifact — create versions via "
                "artifact_service.new_artifact()/new_version()"
            )
        super().__init__(**kw)
