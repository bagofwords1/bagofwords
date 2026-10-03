# Audit Log Stream model
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import json

from cryptography.fernet import Fernet
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from app.models.base import Base, BaseSchema
from app.settings.config import settings

DESTINATIONS = ("datadog", "splunk", "sentinel", "s3", "gcs", "https", "syslog")
STATES = ("active", "inactive", "error", "invalid")


class AuditLogStream(BaseSchema):
    """An organization's delivery target for its audit events.

    ``cursor_seq`` is the ``audit_logs.export_seq`` of the last event this
    stream delivered; the exporter only moves it forward after a successful
    send, so a paused or failing stream resumes with no gaps. ``start_after``
    ("new events only") excludes rows created before the stream was activated.
    """
    __tablename__ = "audit_log_streams"

    organization_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    destination = Column(String(32), nullable=False)
    config = Column(JSON, nullable=False, default=dict)
    # Fernet-encrypted JSON object of secret fields, same scheme as
    # Connection.credentials. Never serialized out of the API.
    secrets = Column(Text, nullable=True)
    action_filter = Column(JSON, nullable=True)  # list of action prefixes; null = all
    state = Column(String(16), nullable=False, default="inactive")
    start_from = Column(String(16), nullable=False, default="now")  # now | beginning

    cursor_seq = Column(BigInteger, nullable=True)
    start_after = Column(DateTime, nullable=True)
    # Bumped by every admin change to where or what the stream sends (config,
    # secrets, action filter). An exporter mid-delivery stops and does not
    # move the cursor once it no longer matches its snapshot.
    # Not updated_at: the exporter's own progress writes bump that.
    config_version = Column(Integer, nullable=False, default=0)

    delivered_count = Column(BigInteger, nullable=False, default=0)
    last_delivered_at = Column(DateTime, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True)

    created_by_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    organization = relationship("Organization")

    def set_secrets(self, values: dict) -> None:
        clean = {k: v for k, v in (values or {}).items() if v not in (None, "")}
        self.secrets = Fernet(settings.bow_config.encryption_key).encrypt(json.dumps(clean).encode()).decode() if clean else None

    def get_secrets(self) -> dict:
        if not self.secrets:
            return {}
        return json.loads(Fernet(settings.bow_config.encryption_key).decrypt(self.secrets.encode()).decode())


class AuditExportState(Base):
    """Single row (id=1): the exporter's sequence stamper lease and the last
    export_seq handed out."""
    __tablename__ = "audit_export_state"

    id = Column(Integer, primary_key=True)
    last_seq = Column(BigInteger, nullable=False, default=0)
    stamp_lease_until = Column(DateTime, nullable=True)
