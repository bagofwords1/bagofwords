from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String

from app.ee.encryption import EncryptedText
from app.models.base import BaseSchema


class QueryExecution(BaseSchema):
    """One query BOW sent (or tried to send) to a connection.

    The same row is three things:

    * the **history** entry a connection manager reads on the connection's
      Queries view: the exact text sent (rendered SQL or the native query —
      never the generated Python), who and what issued it, how long it waited
      and ran, and how it ended;
    * the **slot** behind the per-connection concurrency limit: a query may
      only run while the number of live ``running`` rows for its connection is
      under the limit, so the limit holds across every worker and replica
      (see ``app.data_sources.query_executions``);
    * the **kill target**: a manager marks ``kill_requested_at`` and the worker
      named in ``worker_id`` cancels the statement at the source.

    ``lease_expires_at`` is renewed by the owning worker while the query is
    queued or running. A row whose lease ran out belongs to a worker that died:
    it no longer counts against the limit and reads as ``stale``.

    Attribution columns are plain ids (no foreign keys) so that deleting a
    report or a user never blocks on, or silently rewrites, query history.
    """

    __tablename__ = "query_executions"
    __table_args__ = (
        Index("ix_query_executions_conn_status", "connection_id", "status"),
        Index("ix_query_executions_conn_queued", "connection_id", "queued_at"),
        Index("ix_query_executions_created", "created_at"),
    )

    connection_id = Column(
        String(36),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id = Column(String(36), nullable=True, index=True)
    data_source_id = Column(String(36), nullable=True)
    user_id = Column(String(36), nullable=True)
    report_id = Column(String(36), nullable=True)
    completion_id = Column(String(36), nullable=True)
    agent_execution_id = Column(String(36), nullable=True)
    tool_execution_id = Column(String(36), nullable=True)
    step_id = Column(String(36), nullable=True)
    query_id = Column(String(36), nullable=True)
    entity_id = Column(String(36), nullable=True)
    # Which entry point issued it: agent, query_run, dashboard_refresh.*, ...
    source = Column(String(64), nullable=True)
    client_type = Column(String(64), nullable=True)

    # The rendered query carries the literal values it ran with (parameters,
    # filters), and a source error often quotes them: both are analytical
    # payload, encrypted at rest when data_encryption is on, like
    # Step.applied_params and Entity.code. The DDL stays plain TEXT.
    query_text = Column(EncryptedText, nullable=True)
    policy_version = Column(String(64), nullable=True)
    protected_statement = Column(EncryptedText, nullable=True)
    query_truncated = Column(Integer, nullable=False, default=0)

    # queued | running | success | error | timeout | busy | cancelled | stale
    status = Column(String(16), nullable=False, default="queued")
    queued_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    queued_ms = Column(Float, nullable=True)
    duration_ms = Column(Float, nullable=True)
    rows = Column(Integer, nullable=True)
    error = Column(EncryptedText, nullable=True)
    cancel_outcome = Column(String(255), nullable=True)

    # Retry chain: the tool call's attempt, the code attempt inside it, and the
    # query's position within that code run.
    tool_attempt = Column(Integer, nullable=True)
    code_attempt = Column(Integer, nullable=True)
    seq = Column(Integer, nullable=True)

    # 0 for clients that enforce their own deadline (``_manages_query_deadline``):
    # BOW cannot interrupt their call in flight, so a kill is refused.
    killable = Column(Integer, nullable=False, default=1)

    worker_id = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    kill_requested_at = Column(DateTime, nullable=True)
    killed_by_user_id = Column(String(36), nullable=True)
