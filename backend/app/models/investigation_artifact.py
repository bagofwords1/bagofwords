# Path: backend/app/models/investigation_artifact.py
#
# Handle row (control plane) for the Investigation Artifact Store.
# The payload is an encrypted DuckDB file on shared storage; this row is the
# source of truth for its existence, key, scope, and retention. See
# sandbox-feedback-loop-investigation-artifact-store.md for the full design.

from sqlalchemy import Column, String, BigInteger, Boolean, DateTime, JSON, ForeignKey
from .base import BaseSchema


class InvestigationArtifact(BaseSchema):
    __tablename__ = 'investigation_artifacts'

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    step_id = Column(String(36), ForeignKey('steps.id'), nullable=True, index=True)
    query_id = Column(String(36), ForeignKey('queries.id'), nullable=True, index=True)
    # Loose reference (no FK) — tool executions are persisted asynchronously.
    tool_execution_id = Column(String(36), nullable=True)

    producer = Column(String, nullable=False, default='create_data')   # create_data | rerun | execute_mcp | custom_api
    content_type = Column(String, nullable=False, default='table')     # table | events | text
    schema_json = Column(JSON, nullable=True)                          # [{"name":..,"dtype":..}]
    ts_column = Column(String, nullable=True)                          # detected timestamp column (time_range slicing)

    row_count = Column(BigInteger, nullable=False, default=0)
    byte_size = Column(BigInteger, nullable=False, default=0)
    storage_ref = Column(String, nullable=False)                       # path relative to the store root, never absolute
    format = Column(String, nullable=False, default='duckdb')
    content_sha256 = Column(String, nullable=True)

    # Per-artifact data key, Fernet-wrapped by the master encryption key.
    # NULL after crypto-shredding — the payload is then unrecoverable by design.
    wrapped_key = Column(String, nullable=True)

    status = Column(String, nullable=False, default='published')       # published | tombstoned
    expires_at = Column(DateTime, nullable=True)
    legal_hold = Column(Boolean, nullable=False, default=False)
    cited = Column(Boolean, nullable=False, default=False)             # set by the audit/evidence chain
    superseded_by = Column(String(36), nullable=True)                  # rerun lineage: newer artifact id
