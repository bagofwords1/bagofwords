import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, String, Text

from app.models.base import Base


class AppLog(Base):
    """One line of the application's own stdout/stderr output (System logs).

    Instance-wide on purpose, like ``instance_settings``: these are the
    process's logs, not a tenant's data, so there is no ``organization_id``.
    Written in batches by ``app_log_service`` only while capture is switched
    on, and rotated by age and size (see ``app_log_service.rotate``).

    Deliberately not a ``BaseSchema``: rows are append-only and deleted in
    bulk, so ``updated_at``/``deleted_at`` would be dead weight on what can be
    the largest table in the database.
    """

    __tablename__ = "app_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False)
    # When the line was produced, in naive UTC like every other timestamp here.
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    # Hostname of the process that produced it (pod name on Kubernetes).
    # Indexed through ix_app_logs_instance_timestamp (leading column).
    instance_id = Column(String(255), nullable=False)
    # Python log level name (INFO, ERROR, ...) or NULL for raw stream writes
    # such as print() and tracebacks that bypass logging.
    level = Column(String(16), nullable=True)
    logger = Column(String(255), nullable=True)
    # "stdout" or "stderr": the stream the line went to on the console.
    stream = Column(String(8), nullable=False, default="stdout")
    message = Column(Text, nullable=False)

    __table_args__ = (
        Index("ix_app_logs_instance_timestamp", "instance_id", "timestamp"),
    )
