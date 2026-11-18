"""Request/response contracts for importing an agent from an export bundle.

The bundle is the zip produced by ``GET /data_sources/{id}/instructions/export``
(``agent.yaml`` + ``instructions/**.md`` + ``evals/*.yaml`` + ``queries/*.yaml``).
Import is two calls on the same uploaded file: ``preview`` (read-only, answers
"what is inside and how does it fit this organization?") and ``import``
(creates a NEW agent). The connection names inside the bundle belong to the
environment it was exported from, so the client sends a ``connection_map``
from each bundle connection name to a connection id in this organization, or
``null`` to leave that connection (and its tables and tools) out.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class AgentBundleImportOptions(BaseModel):
    # Name for the new agent. Defaults to the bundle's agent name.
    name: Optional[str] = None
    # bundle connection name -> connection id in this org (None = skip).
    # A bundle connection missing from the map falls back to the suggestion
    # the preview made for it (same name in this org).
    connection_map: Dict[str, Optional[str]] = Field(default_factory=dict)
    include_instructions: bool = True
    include_evals: bool = True
    include_queries: bool = True
    include_conversation_starters: bool = True
    # False (default): instructions and queries land as drafts for review.
    # True: keep each file's own status (published stays published).
    publish_instructions: bool = False
    # Optional agent folder in /agents to create the agent in.
    folder_id: Optional[str] = None


class BundleConnectionPreview(BaseModel):
    """One connection named by the bundle, and how it maps to this org."""

    name: str
    # Active tables the bundle selects on this connection.
    tables_total: int = 0
    # Tool overlays the bundle sets on this connection.
    tools_total: int = 0
    # The connection id the import would use (the client's choice, else the
    # same-name suggestion). None = not mapped yet / skipped.
    mapped_connection_id: Optional[str] = None
    # True when the mapping was suggested by the server (same name), not sent.
    suggested: bool = False
    # How many of the bundle's tables exist in the mapped connection. None
    # while unmapped, or while the mapped connection has no indexed tables.
    tables_found: Optional[int] = None
    # The mapped connection has no indexed tables yet (still indexing).
    indexing_pending: bool = False
    # A sample of bundle tables the mapped connection does not have.
    missing_tables: List[str] = Field(default_factory=list)
    # Set when the caller cannot build agents on the mapped connection.
    error: Optional[str] = None


class AgentBundleCounts(BaseModel):
    instructions: int = 0
    folders: int = 0
    evals: int = 0
    eval_cases: int = 0
    queries: int = 0
    conversation_starters: int = 0
    members: int = 0


class AgentBundlePreview(BaseModel):
    agent_name: str
    # agent_name if free, else the first free "<name> (imported)" variant.
    suggested_name: str
    name_taken: bool = False
    description: Optional[str] = None
    connections: List[BundleConnectionPreview] = Field(default_factory=list)
    counts: AgentBundleCounts = Field(default_factory=AgentBundleCounts)
    # Members (user emails / group names) that do not exist in this org and
    # will be left out.
    members_unresolved: List[str] = Field(default_factory=list)
    # Non-fatal notes about the bundle itself (unreadable files, ...).
    warnings: List[str] = Field(default_factory=list)


class AgentBundleImportResult(BaseModel):
    data_source_id: str
    name: str
    instructions_created: int = 0
    instructions_failed: int = 0
    folders_created: int = 0
    references_resolved: int = 0
    references_unresolved: int = 0
    evals_created: int = 0
    queries_created: int = 0
    tables_active: int = 0
    tables_missing: int = 0
    warnings: List[str] = Field(default_factory=list)
