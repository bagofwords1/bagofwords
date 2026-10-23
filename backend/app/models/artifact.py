from sqlalchemy import Boolean, Column, DateTime, String, ForeignKey, Index, Integer, JSON, Text, UniqueConstraint, select
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

    Parents backfilled by migration artver01 (one per pre-migration version
    row) reuse that version's id; parents made since always get a fresh one.
    The frontend reads that as "pre-migration history" — never pass a
    version id where an artifact id is expected: it only happens to resolve
    on old data.
    """
    __tablename__ = 'artifacts'
    # /d/{slug} names one dashboard in the whole installation (dashfc01); an
    # old /r/{report_id} link leads to at most one dashboard (dashfc03).
    __table_args__ = (
        Index('uq_artifacts_slug', 'slug', unique=True),
        Index('uq_artifacts_legacy_report_id', 'legacy_report_id', unique=True),
    )

    # The editing conversation. Nullable since dashfc01: a published dashboard
    # outlives the conversation it was made in (dashboards project, phase 7);
    # until then every row still has one.
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    # lazy="select", not selectin: the factory and the service load parents
    # with a bare db.get() to rename / soft-delete them, and a selectin here
    # would pull the Report and its whole selectin graph (completions,
    # queries -> steps, widgets, ...) into every such call.
    # foreign_keys: reports.source_artifact_id is a second path between the
    # two tables.
    report = relationship("Report", back_populates="artifacts", lazy="select",
                          foreign_keys="Artifact.report_id")

    # Organization for multi-tenancy
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)

    # User who started this artifact (each version carries its own author)
    created_by = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)

    # Mode: 'page' (dashboard), 'slides' or 'doc' — fixed for life
    mode = Column(String(20), nullable=False, default='page', index=True)

    title = Column(String(255), nullable=True, default="Untitled Artifact")

    # ── The dashboard's own settings and sharing (dashfc01) ──────────────
    # They mirror the conversation-level columns on `reports` (same
    # defaults). Since phase 4 the /d routes read them and /api/dashboards
    # writes them; /r keeps reading the conversation's until the data
    # migration (docs/design/dashboards-first-class.md). The schedule ones
    # refresh the published release (phase 6).
    owner_id = Column(String(36), ForeignKey('users.id', name='fk_artifacts_owner_id'), nullable=True, index=True)
    project_id = Column(String(36), ForeignKey('projects.id', name='fk_artifacts_project_id'),
                        nullable=True, index=True)
    visibility = Column(String(20), nullable=False, default='none', server_default='none')
    slug = Column(String(255), nullable=True)
    shared_run_identity = Column(String(20), nullable=False, default='viewer', server_default='viewer')
    include_data_tab = Column(Boolean, nullable=False, default=True, server_default='1')
    chat_enabled = Column(Boolean, nullable=False, default=False, server_default='0')
    chat_data_source_ids = Column(JSON, nullable=True)
    chat_model_id = Column(String(36), nullable=True)  # llm_models id or 'org_default'; no FK
    cron_schedule = Column(String, nullable=True)
    refresh_on_view = Column(Boolean, nullable=False, default=True, server_default='1')
    last_run_at = Column(DateTime, nullable=True)
    # How the last refresh of the published release went (dashfc05):
    # 'success' | 'failed', and why it failed. last_run_at: the last success.
    last_run_status = Column(String(20), nullable=True)
    last_run_error = Column(Text, nullable=True)
    notification_subscribers = Column(JSON, nullable=True)
    # The live release. artifact_releases.artifact_id points back here, so the
    # key is created after both tables (use_alter) and cleared, not blocked,
    # when its release is deleted.
    published_release_id = Column(
        String(36),
        ForeignKey('artifact_releases.id', name='fk_artifacts_published_release_id',
                   ondelete='SET NULL', use_alter=True),
        nullable=True, index=True,
    )
    # The conversation whose old /r/{report_id} link leads here (dashfc03).
    # No foreign key: the value outlives the conversation it names.
    legacy_report_id = Column(String(36), nullable=True)
    # Collaboration (collab01): whether editors may change the sharing, and the
    # dashboard's notes -- its README, carried into every editing conversation.
    editors_can_share = Column(Boolean, nullable=False, default=True, server_default='1')
    notes = Column(Text, nullable=True)

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

    # Collaboration (collab01). The version this one grew from: the previous
    # version in its conversation, or the version a conversation joining the
    # dashboard copied (a soft reference: no foreign key, it outlives the row).
    # Publish reads the chain to tell whether the live release is behind it.
    base_version_id = Column(String(36), nullable=True)
    # The named draft it belongs to; NULL is the dashboard's implicit draft.
    draft_id = Column(String(36), ForeignKey('artifact_drafts.id', name='fk_artifact_versions_draft_id',
                                             ondelete='SET NULL'), nullable=True, index=True)
    # Each chart's fingerprint when this version was made:
    # {element_key: {"fp": ..., "title": ...}} (dashboard_collab.snapshot_elements).
    # Comparing versions by it says who changed which chart.
    elements = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint('artifact_id', 'version', name='uq_artifact_versions_artifact_version'),
    )

    # Read-through to the parent (declared after artifact_id on purpose:
    # the correlated subquery binds to the column defined above).
    #
    # expire_on_flush=False is load-bearing: by default SQLAlchemy expires
    # SQL-expression properties whenever the instance is flushed with an
    # UPDATE (it cannot know whether the subquery's value changed). Version
    # rows are routinely mutated after creation — thumbnail_path, screenshot,
    # status, pptx_path — and the next .title/.mode access would then
    # lazy-load and raise MissingGreenlet under async (this crashed
    # "Use this version" on any artifact that has a thumbnail). The parent's
    # title only changes through the factory, which refreshes explicitly, so
    # keeping the loaded value across unrelated flushes is correct.
    title = column_property(
        select(Artifact.title)
        .where(Artifact.id == artifact_id)
        .correlate_except(Artifact)
        .scalar_subquery(),
        expire_on_flush=False,
    )
    mode = column_property(
        select(Artifact.mode)
        .where(Artifact.id == artifact_id)
        .correlate_except(Artifact)
        .scalar_subquery(),
        expire_on_flush=False,
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


# Register the dashfc01 tables wherever artifacts are loaded (metadata walks,
# e.g. the agent-delete foreign-key guard, must see them).
from app.models import artifact_release  # noqa: E402,F401
