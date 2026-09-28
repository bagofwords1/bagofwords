"""Agent Lists — typed, per-agent collections of records.

A List is a named schema (``fields``) owned by an agent (DataSource). The
agent fills it through a natively-registered ``submit_<slug>`` tool whose input
schema is compiled from ``fields`` (see ``app.services.agent_lists``). Rows
accumulate across reports, are upserted on the list's key field, and carry
per-field evidence plus provenance (report / tool execution / schema version).

Access follows the owning agent: anyone who can VIEW the agent can read the
list and its rows; MANAGE on the agent is required to change the schema or
edit rows.
"""
from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, JSON, String, Text

from app.ee.encryption import EncryptedJSON
from app.models.base import BaseSchema


class AgentList(BaseSchema):
    __tablename__ = "agent_lists"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    data_source_id = Column(String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    name = Column(String(255), nullable=False)
    # Stable machine name used for the tool name (submit_<slug>) and table
    # name (bow.<agent>.lists.<slug>). Unique among live lists of one agent.
    slug = Column(String(64), nullable=False)
    description = Column(Text, nullable=True)
    # [{id, name, type, description, required, enum, items, unit, method}]
    fields = Column(JSON, nullable=False, default=list)
    key_field_id = Column(String(64), nullable=True)
    require_evidence = Column(Boolean, nullable=False, default=False)
    # Who may save rows through the agent's submit_<list> tool. Off: only users
    # who can MANAGE the agent (the same bar as editing rows by hand). On:
    # anyone who can use (view) the agent — crowd-sourced extraction.
    allow_viewer_submissions = Column(Boolean, nullable=False, default=False)
    # When on, a value a person edited is locked: agent submissions skip it until
    # unlocked. Off by default — edits are still recorded, the agent may overwrite.
    keep_human_edits = Column(Boolean, nullable=False, default=False)
    # Bumped on breaking schema changes only (see classify_schema_change).
    version = Column(Integer, nullable=False, default=1)


class AgentListRow(BaseSchema):
    __tablename__ = "agent_list_rows"
    __table_args__ = (
        Index("ix_agent_list_rows_list_key", "list_id", "key_value", unique=True),
    )

    list_id = Column(String(36), ForeignKey("agent_lists.id", ondelete="CASCADE"), nullable=False, index=True)
    # Normalized key-field value for upserts (null when the list has no key
    # or the record did not carry one).
    key_value = Column(String(512), nullable=True)
    # {field_id: {value, status, evidence: [...], note, source, edited_by, edited_at}}
    values = Column(EncryptedJSON, nullable=False, default=dict)
    schema_version = Column(Integer, nullable=False, default=1)
    row_version = Column(Integer, nullable=False, default=1)
    # Field ids a human edited; the agent never overwrites these.
    locked_fields = Column(JSON, nullable=False, default=list)

    report_id = Column(String(36), ForeignKey("reports.id", ondelete="SET NULL"), nullable=True, index=True)
    tool_execution_id = Column(String(36), nullable=True)
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    updated_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)


class AgentListRowRevision(BaseSchema):
    __tablename__ = "agent_list_row_revisions"

    row_id = Column(String(36), ForeignKey("agent_list_rows.id", ondelete="CASCADE"), nullable=False, index=True)
    list_id = Column(String(36), ForeignKey("agent_lists.id", ondelete="CASCADE"), nullable=False, index=True)
    # 'agent' | 'user'
    actor_type = Column(String(16), nullable=False)
    actor_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    report_id = Column(String(36), nullable=True)
    tool_execution_id = Column(String(36), nullable=True)
    # 'insert' | 'update' | 'revert' | 'lock' | 'unlock'
    action = Column(String(16), nullable=False, default="update")
    # {field_id: {"before": envelope|None, "after": envelope|None}}
    changed = Column(EncryptedJSON, nullable=False, default=dict)
