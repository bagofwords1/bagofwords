"""Agent Lists — typed collections of records, shared by one or more agents.

A List is a named schema (``fields``) linked to one or more agents
(DataSource) through ``agent_list_data_sources``. Each linked agent fills it
through a natively-registered ``submit_<slug>`` tool whose input schema is
compiled from ``fields`` (see ``app.services.agent_lists``). Rows accumulate
across reports, are upserted on the list's key field, and carry per-field
evidence plus provenance (report / tool execution / schema version).

Access follows the linked agents: anyone who can VIEW one of them can read
the list and its rows; MANAGE on one of them is required to edit rows, and
MANAGE on all of them to change the schema.
"""
from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, JSON, String, Table, Text

from app.ee.encryption import EncryptedJSON
from app.models.base import BaseSchema


# Which agents a list belongs to. Every live list has at least one row here,
# and always one for its home agent (``AgentList.data_source_id``).
agent_list_data_sources = Table(
    "agent_list_data_sources",
    BaseSchema.metadata,
    Column("list_id", String(36), ForeignKey("agent_lists.id", ondelete="CASCADE"), primary_key=True),
    Column("data_source_id", String(36), ForeignKey("data_sources.id", ondelete="CASCADE"),
           primary_key=True, index=True),
)


class AgentList(BaseSchema):
    __tablename__ = "agent_lists"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    # Home agent: the one the list was created on. Kept for ordering and as the
    # default naming context; membership is ``agent_list_data_sources``. When
    # the home agent is deleted while the list is still shared, another linked
    # agent becomes home (see service.detach_agent_lists).
    data_source_id = Column(String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    name = Column(String(255), nullable=False)
    # Stable machine name used for the tool name (submit_<slug>) and table
    # name (bow.<agent>.lists.<slug>). Unique among the live lists of each
    # agent the list is linked to.
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
