"""What happened in an app while its editor (or BOW's own preview) used it.

One row per event: an SDK request and how it ended, a stream's end, a
filter change, a render or console error, a click breadcrumb. The agent
reads it (app/services/artifact_activity.py) so "it doesn't work" needs no
pasted errors. Never recorded for viewers or inside host applications.
Short-lived: see ``RETENTION_DAYS`` / ``KEEP_PER_REPORT`` in the service.
"""
from sqlalchemy import JSON, Column, ForeignKey, Index, Integer, String

from app.ee.encryption import EncryptedJSON
from app.models.base import BaseSchema

SOURCES = ("editor", "preview")
KINDS = ("request", "stream", "filter", "error", "console", "click")


class ArtifactActivityEvent(BaseSchema):
    __tablename__ = "artifact_activity_events"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    artifact_id = Column(String(36), nullable=False)
    artifact_version = Column(Integer, nullable=True)
    report_id = Column(String(36), nullable=True)
    user_id = Column(String(36), nullable=True)
    # One page load (editor) or one preview; ``seq`` orders events inside it.
    session_id = Column(String(64), nullable=False)
    seq = Column(Integer, nullable=False, default=0)
    source = Column(String(16), nullable=False, default="editor")
    kind = Column(String(16), nullable=False)
    op = Column(String(64), nullable=True)
    status = Column(String(16), nullable=True)
    error_code = Column(String(80), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    detail = Column(JSON, nullable=True)
    # The agent turn that first saw this event (null until then).
    shown_in_completion_id = Column(String(36), nullable=True)

    __table_args__ = (
        Index("ix_artifact_activity_report_created", "report_id", "created_at"),
        Index("ix_artifact_activity_artifact_created", "artifact_id", "created_at"),
        Index("ix_artifact_activity_created", "created_at"),
    )


class ArtifactActivityPayload(BaseSchema):
    """What an event carried: the request the app made, what came back, the
    stream's events. Private to the person whose session produced it
    (``user_id``), encrypted at rest like step results, redacted for PII on
    the way in, bounded, and kept for ``PAYLOAD_RETENTION_HOURS`` only
    (services/artifact_activity.py). The agent sees it only through the
    org's data gate (shape only when the model may not see data)."""
    __tablename__ = "artifact_activity_payloads"

    event_id = Column(String(36), ForeignKey("artifact_activity_events.id", ondelete="CASCADE"), nullable=False,
                      unique=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(String(36), nullable=False)
    request = Column(EncryptedJSON(none_as_null=True), nullable=True)
    response = Column(EncryptedJSON(none_as_null=True), nullable=True)
    stream = Column(EncryptedJSON(none_as_null=True), nullable=True)

    __table_args__ = (
        Index("ix_artifact_activity_payloads_created", "created_at"),
    )
