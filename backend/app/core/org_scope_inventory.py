"""Where every mapped model sits relative to the organization boundary.

A model with an organization column (``organization_id`` or a foreign key to
``organizations.id``) is *org-scoped* and is derived, not listed: the query
guard (``app.core.org_scope_guard``) checks it. Every other model must be
classified here, so a new table cannot appear without someone deciding how
its rows are tied to an organization
(``tests/unit/test_org_scope_inventory.py`` fails until it is).

- ``PARENT_SCOPED``: no organization column; a row belongs to the organization
  of the listed parent model(s), reached through a non-null foreign key. A
  read by id must go through (or be checked against) that parent. Parents may
  themselves be parent-scoped; the chain must end at an org-scoped model.
- ``GLOBAL``: deliberately shared by the whole deployment.
- ``UNCLASSIFIED_REVIEW``: nobody has decided yet. Each entry says why it is
  not obvious. Move entries out of here; do not add to it lightly.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

ORG_ROOT = "Organization"

PARENT_SCOPED: Dict[str, Tuple[str, ...]] = {
    # models only imported by the code path that uses them
    "EmailDeliveryClaim": ("AgentExecution",),
    "FileIndexFile": ("Connection",),
    "FileIndexSection": ("FileIndexFile",),
    "ReportContextState": ("Report",),
    "ReportView": ("Report",),
    # agent lists
    "AgentListRow": ("AgentList",),
    "AgentListRowRevision": ("AgentList",),
    # artifacts
    "ArtifactConversation": ("Artifact",),
    "ArtifactFileBinding": ("Artifact",),
    "ArtifactHostApplication": ("Artifact",),
    "ArtifactMutation": ("Artifact",),
    "ArtifactPresence": ("Artifact",),
    "ArtifactPublication": ("Artifact",),
    "ArtifactRecord": ("ArtifactResource",),
    "ArtifactRecordIndex": ("ArtifactResource",),
    "ArtifactShare": ("Artifact",),
    "ArtifactSlugHistory": ("Artifact",),
    "ArtifactStar": ("Artifact",),
    "ArtifactView": ("Artifact",),
    "ArtifactViewDay": ("Artifact",),
    # reports and what hangs off them
    "Completion": ("Report",),
    "CompletionBlock": ("Completion",),
    "DashboardLayoutVersion": ("Report",),
    "Mention": ("Report",),
    "ReportAdditionalFolder": ("Report",),
    "ReportShare": ("Report",),
    "ReportStar": ("Report",),
    "ScheduledPrompt": ("Report",),
    "TextWidget": ("Report",),
    "Widget": ("Report",),
    "Visualization": ("Query",),
    # agent executions
    "ContextSnapshot": ("AgentExecution",),
    "PlanDecision": ("AgentExecution",),
    "ToolExecution": ("AgentExecution",),
    # connections and data sources
    "ConnectionIndexing": ("Connection",),
    "ConnectionRateLimitCounter": ("Connection",),
    "ConnectionTable": ("Connection",),
    "ConnectionTool": ("Connection",),
    "DataSourceConnectionSetting": ("DataSource",),
    "DataSourceConnectionTool": ("DataSource",),
    "DataSourceMembership": ("DataSource",),
    "DataSourceTable": ("DataSource",),
    "UserConnectionColumn": ("UserConnectionTable",),
    "UserConnectionTable": ("Connection",),
    "UserConnectionTool": ("Connection",),
    "UserConnectionToolPreference": ("ConnectionTool",),
    "UserDataSourceColumn": ("UserDataSourceTable",),
    "UserDataSourceTable": ("DataSource",),
    # instructions and builds
    "BuildContent": ("InstructionBuild",),
    "InstructionDirectoryPlacement": ("Instruction",),
    "InstructionReference": ("Instruction",),
    "InstructionVersion": ("Instruction",),
    # files
    "FileTag": ("File",),
    "SheetSchema": ("File",),
    # identity within an organization
    "GroupMembership": ("Group",),
    # connected applications
    "HostApplication": ("ConnectedApplication",),
    "HostAssertionReplay": ("HostApplication",),
    "OAuthClient": ("ConnectedApplication",),
    # prompts and evals
    "PromptRun": ("Prompt",),
    "TestCase": ("TestSuite",),
    "TestResult": ("TestCase",),
}

GLOBAL: FrozenSet[str] = frozenset({
    "InstanceSettings",
    "LoginExchangeCode",  # one-time sign-in code for a global user account
    "OAuthAccount",   # sign-in identity of a global user account
    "PendingBlobDelete",  # storage paths of already-deleted files, retried by the nightly job; read by no API
    "SAMLRequest",    # sign-in state, before any organization is known
    "User",           # one account, a membership per organization
})

UNCLASSIFIED_REVIEW: Dict[str, str] = {
    "ArtifactRateBucket": "no foreign keys; keyed by an opaque scope hash",
    "AuditExportState": "no foreign keys; probably deployment-wide export cursor",
    "OfficeJsPendingResult": "only nullable links (completion, user)",
    "Step": "both parent links are nullable (widget, query)",
    "TestRun": "no non-null parent; suites are a comma-separated id list",
}
