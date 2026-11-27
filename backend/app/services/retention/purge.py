"""Hard-delete a set of rows and everything that belongs to them.

Retention deletes for good. A report owns a deep tree — completions, agent
executions, plans, plan decisions, tool executions, completion blocks,
context snapshots, widgets, steps, queries, sub-agents, notes, … — and most
of the foreign keys into that tree have no ``ON DELETE CASCADE`` (SQLite
does not even enforce them). So the purge walks the schema itself:

* ``POLICY`` says, for every reference into a table the purge may delete,
  whether the referencing row is **deleted with it** (``DELETE``: it belongs
  to the deleted row) or **kept with the link cleared** (``NULL``: it is
  knowledge, billing or history that outlives it — an instruction learned
  from a run, an LLM usage record, an eval case).
* ``closure`` follows the ``DELETE`` edges from the roots to every row that
  goes with them.
* ``execute`` clears the ``NULL`` links, breaks cycles, and deletes every
  row of the closure, children first.

A reference with no policy is an error, never a guess: ``closure`` raises
``UnhandledReference`` and nothing is deleted. The guard test
``tests/unit/retention/test_policy_coverage.py`` walks ``Base.metadata`` so a
new model that points into the tree fails CI until it is given a policy.

References are real foreign keys plus ``LOGICAL_REFERENCES``: id columns
without a constraint that still point at a deleted row.
"""
from __future__ import annotations

import functools
import importlib
import logging
import pkgutil
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import Table, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DELETE = "delete"
NULL = "null"

# (child table, child column) -> policy, for references into a table the
# purge may delete. Grouped by the parent table they point at.
POLICY: dict[tuple[str, str], str] = {
    # -- reports ---------------------------------------------------------
    ("agent_executions", "report_id"): DELETE,
    ("artifact_versions", "report_id"): DELETE,
    ("artifacts", "report_id"): DELETE,          # a dashboard never published goes with its conversation
    ("completions", "report_id"): DELETE,
    ("queries", "report_id"): DELETE,
    ("reports", "forked_from_id"): NULL,         # another conversation forked from this one stays
    ("test_runs", "origin_report_id"): NULL,
    ("agent_checkins", "report_id"): DELETE,
    ("dashboard_layout_versions", "report_id"): DELETE,
    ("folder_report_knowledge", "report_id"): DELETE,
    ("folder_report_sources", "report_id"): DELETE,
    ("instruction_stats", "report_id"): NULL,
    ("instruction_usage_events", "report_id"): NULL,
    ("mentions", "report_id"): DELETE,
    ("notes", "report_id"): DELETE,
    ("plans", "report_id"): DELETE,
    ("report_additional_folders", "report_id"): DELETE,
    ("report_context_states", "report_id"): DELETE,
    ("report_data_source_association", "report_id"): DELETE,
    ("report_stars", "report_id"): DELETE,
    ("report_views", "report_id"): DELETE,
    ("scheduled_prompts", "report_id"): DELETE,  # only inactive ones: an active schedule keeps the report
    ("step_user_results", "report_id"): DELETE,
    ("sub_agent_waves", "report_id"): DELETE,
    ("text_widgets", "report_id"): DELETE,
    ("tool_confirmations", "report_id"): DELETE,
    ("visualizations", "report_id"): DELETE,
    ("widgets", "report_id"): DELETE,
    ("agent_list_rows", "report_id"): NULL,      # list data the agent collected outlives the chat
    ("artifact_conversations", "report_id"): DELETE,
    ("file_references", "report_id"): DELETE,
    ("instruction_feedback_events", "report_id"): NULL,
    ("prompt_runs", "report_id"): NULL,
    ("report_shares", "report_id"): DELETE,
    ("webhooks", "report_id"): DELETE,           # only inactive ones: an active webhook keeps the report
    ("llm_usage_records", "report_id"): NULL,    # usage and cost history
    ("report_file_association", "report_id"): DELETE,
    ("sub_agent_runs", "report_id"): DELETE,
    ("table_feedback_events", "report_id"): NULL,
    ("table_stats", "report_id"): NULL,
    ("table_usage_events", "report_id"): NULL,
    ("test_results", "report_id"): NULL,
    ("sub_agent_messages", "report_id"): DELETE,
    # -- completions -----------------------------------------------------
    ("agent_executions", "completion_id"): DELETE,
    ("artifact_versions", "completion_id"): NULL,
    ("completions", "parent_id"): NULL,
    ("agent_checkins", "run_completion_id"): NULL,
    ("agent_checkins", "source_completion_id"): NULL,
    ("completion_feedbacks", "completion_id"): DELETE,
    ("mentions", "completion_id"): DELETE,
    ("officejs_pending_results", "completion_id"): DELETE,
    ("plans", "completion_id"): DELETE,
    ("report_context_states", "covers_until_completion_id"): NULL,
    ("sub_agent_waves", "parent_completion_id"): DELETE,
    ("sub_agent_waves", "fan_in_completion_id"): NULL,
    ("report_file_association", "completion_id"): NULL,
    ("test_cases", "source_completion_id"): NULL,  # an eval case built from a run stays
    ("completion_blocks", "completion_id"): DELETE,
    ("sub_agent_runs", "parent_completion_id"): DELETE,
    ("sub_agent_runs", "child_completion_id"): NULL,
    ("sub_agent_runs", "child_head_completion_id"): NULL,
    ("test_results", "head_completion_id"): DELETE,
    # -- agent_executions ------------------------------------------------
    ("agent_executions", "parent_agent_execution_id"): NULL,
    ("instruction_builds", "agent_execution_id"): NULL,
    ("instructions", "agent_execution_id"): NULL,  # an instruction learned from a run stays
    ("context_snapshots", "agent_execution_id"): DELETE,
    ("email_delivery_claims", "agent_execution_id"): DELETE,
    ("execution_exposed_agents", "execution_id"): DELETE,
    ("folder_execution_agents", "execution_id"): DELETE,
    ("notes", "agent_execution_id"): NULL,
    ("sub_agent_waves", "parent_execution_id"): NULL,
    ("plan_decisions", "agent_execution_id"): DELETE,
    ("test_cases", "source_agent_execution_id"): NULL,
    ("tool_executions", "agent_execution_id"): DELETE,
    ("completion_blocks", "agent_execution_id"): DELETE,
    ("sub_agent_runs", "parent_execution_id"): NULL,
    ("sub_agent_runs", "child_execution_id"): NULL,
    ("test_results", "agent_execution_id"): NULL,
    # -- run tree below executions --------------------------------------
    ("plan_decisions", "context_snapshot_id"): NULL,
    ("tool_executions", "context_snapshot_id"): NULL,
    ("tool_executions", "plan_decision_id"): NULL,
    ("completion_blocks", "plan_decision_id"): NULL,
    ("completion_blocks", "tool_execution_id"): NULL,
    ("sub_agent_runs", "parent_tool_execution_id"): NULL,
    ("instruction_feedback_events", "completion_feedback_id"): DELETE,
    ("test_cases", "source_feedback_id"): NULL,
    ("table_feedback_events", "completion_feedback_id"): DELETE,
    ("sub_agent_runs", "report_note_id"): NULL,
    ("completions", "scheduled_prompt_id"): NULL,
    ("sub_agent_runs", "wave_id"): DELETE,
    ("sub_agent_messages", "wave_id"): DELETE,
    ("sub_agent_messages", "sub_agent_id"): DELETE,
    ("sub_agent_messages", "sender_sub_agent_id"): NULL,
    ("sub_agent_messages", "reply_to"): NULL,
    ("completions", "webhook_id"): NULL,
    ("webhook_data_source_association", "webhook_id"): DELETE,
    # -- widgets, queries, steps ------------------------------------------
    ("completions", "widget_id"): NULL,
    ("queries", "widget_id"): NULL,
    ("steps", "widget_id"): DELETE,
    ("tool_executions", "created_widget_id"): NULL,
    ("steps", "query_id"): DELETE,
    ("visualizations", "query_id"): DELETE,
    ("completions", "step_id"): NULL,
    ("queries", "default_step_id"): NULL,
    ("entities", "source_step_id"): NULL,          # a saved entity outlives the step it came from
    ("step_user_results", "step_id"): DELETE,
    ("tool_executions", "created_step_id"): NULL,
    ("table_feedback_events", "step_id"): NULL,
    ("table_usage_events", "step_id"): DELETE,     # NOT NULL: the event cannot outlive its step
    # -- artifacts -------------------------------------------------------
    ("artifact_releases", "artifact_id"): DELETE,
    ("artifact_versions", "artifact_id"): DELETE,
    ("reports", "source_artifact_id"): NULL,
    ("reports", "forked_from_artifact_id"): NULL,  # a fork is its owner's conversation
    ("artifact_drafts", "artifact_id"): DELETE,
    ("artifact_mutations", "artifact_id"): DELETE,
    ("artifact_presence", "artifact_id"): DELETE,
    ("artifact_publications", "artifact_id"): DELETE,
    ("artifact_resources", "artifact_id"): DELETE,
    ("artifact_slug_history", "artifact_id"): DELETE,
    ("artifact_stars", "artifact_id"): DELETE,
    ("artifact_view_days", "artifact_id"): DELETE,
    ("artifact_views", "artifact_id"): DELETE,
    ("dashboard_access_requests", "artifact_id"): DELETE,
    ("dashboard_refresh_runs", "artifact_id"): DELETE,
    ("artifact_conversations", "artifact_id"): DELETE,
    ("artifact_shares", "artifact_id"): DELETE,
    ("artifact_file_bindings", "artifact_id"): DELETE,
    ("artifact_host_applications", "artifact_id"): DELETE,
    ("artifact_releases", "source_version_id"): NULL,
    ("artifact_publications", "version_id"): DELETE,
    ("artifacts", "published_release_id"): NULL,
    ("queries", "artifact_release_id"): DELETE,
    ("artifact_release_data_source_association", "artifact_release_id"): DELETE,
    ("visualizations", "artifact_release_id"): DELETE,
    ("artifact_release_file_association", "artifact_release_id"): DELETE,
    ("artifact_versions", "draft_id"): NULL,
    ("artifact_conversations", "draft_id"): NULL,
    ("artifact_records", "resource_id"): DELETE,
    ("artifact_file_bindings", "resource_id"): DELETE,
    ("artifact_record_indexes", "resource_id"): DELETE,
    ("artifact_record_indexes", "record_id"): DELETE,
    ("artifact_activity_payloads", "event_id"): DELETE,
}

# Id columns with no foreign key that still point at a row the purge may
# delete: (child table, child column, parent table).
LOGICAL_REFERENCES: list[tuple[str, str, str]] = [
    ("artifact_activity_events", "artifact_id", "artifacts"),
    ("artifact_activity_events", "report_id", "reports"),
    ("artifact_activity_events", "shown_in_completion_id", "completions"),
    ("artifact_presence", "report_id", "reports"),
    ("artifact_presence", "completion_id", "completions"),
    ("agent_list_rows", "tool_execution_id", "tool_executions"),
    ("agent_list_row_revisions", "report_id", "reports"),
    ("agent_list_row_revisions", "tool_execution_id", "tool_executions"),
    ("query_executions", "report_id", "reports"),
    ("query_executions", "completion_id", "completions"),
    ("query_executions", "agent_execution_id", "agent_executions"),
    ("query_executions", "tool_execution_id", "tool_executions"),
    ("llm_usage_records", "agent_execution_id", "agent_executions"),
    ("agent_executions", "parent_tool_execution_id", "tool_executions"),
    ("tool_confirmations", "system_completion_id", "completions"),
    ("tool_confirmations", "head_completion_id", "completions"),
    ("tool_confirmations", "tool_execution_id", "tool_executions"),
    ("completions", "source_report_id", "reports"),
    ("dashboard_refresh_runs", "report_id", "reports"),
    ("artifacts", "legacy_report_id", "reports"),
]
LOGICAL_POLICY: dict[tuple[str, str], str] = {
    ("artifact_activity_events", "artifact_id"): DELETE,
}

# The tables a purge starts from. Files are never deleted by the walk: the
# links to them are, and files nothing links to any more are then purged
# through file_service.purge_files (rows, tags, schemas and bytes).
ROOT_TABLES = ("reports", "artifacts")

CHUNK = 400


class UnhandledReference(RuntimeError):
    """A reference into the purge tree has no policy. Nothing was deleted."""


# Model modules the app never imports: no migration creates their tables and
# importing them breaks mapper configuration (``Application`` names a class
# that no longer exists).
_NOT_LOADED = {"app.models.application"}


def load_all_models() -> None:
    """Import every model module so ``Base.metadata`` holds every table."""
    import app.models as models_pkg

    for info in pkgutil.iter_modules(models_pkg.__path__, models_pkg.__name__ + "."):
        if info.name not in _NOT_LOADED:
            importlib.import_module(info.name)


@dataclass(frozen=True)
class Reference:
    child: Table
    column: str
    parent: str
    nullable: bool
    policy: Optional[str]
    logical: bool = False


@functools.lru_cache(maxsize=None)
def _metadata():
    from app.models.base import Base

    load_all_models()
    return Base.metadata


@functools.lru_cache(maxsize=None)
def references_into(parent: str) -> tuple[Reference, ...]:
    md = _metadata()
    refs: list[Reference] = []
    for table in md.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == parent:
                col = fk.parent
                refs.append(Reference(table, col.name, parent, bool(col.nullable),
                                      POLICY.get((table.name, col.name))))
    for child, column, target in LOGICAL_REFERENCES:
        if target != parent or child not in md.tables:
            continue
        table = md.tables[child]
        col = table.c[column]
        refs.append(Reference(table, column, parent, bool(col.nullable),
                              LOGICAL_POLICY.get((child, column), NULL), logical=True))
    return tuple(refs)


def deletable_tables() -> set[str]:
    """Every table a purge may delete rows from: the roots, then whatever a
    DELETE edge reaches."""
    seen, queue = set(ROOT_TABLES), deque(ROOT_TABLES)
    while queue:
        parent = queue.popleft()
        for ref in references_into(parent):
            if ref.policy == DELETE and ref.child.name not in seen:
                seen.add(ref.child.name)
                queue.append(ref.child.name)
    return seen


def _id_column(table: Table):
    pk = list(table.primary_key.columns)
    if len(pk) == 1 and pk[0].name == "id":
        return pk[0]
    return None


def _chunks(values: Iterable[str]) -> Iterable[list[str]]:
    values = list(values)
    for i in range(0, len(values), CHUNK):
        yield values[i:i + CHUNK]


@dataclass
class Closure:
    """The rows a purge deletes. ``ids`` for tables with an ``id`` key;
    ``links`` for link tables (deleted by the parent ids they point at)."""

    ids: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    links: list[tuple[Reference, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {t: len(v) for t, v in sorted(self.ids.items()) if v}


async def closure(db: AsyncSession, roots: dict[str, Iterable[str]]) -> Closure:
    md = _metadata()
    out = Closure()
    queue: deque[tuple[str, set[str]]] = deque()
    for table, ids in roots.items():
        new = {str(i) for i in ids} - out.ids[table]
        if new:
            out.ids[table] |= new
            queue.append((table, new))
    while queue:
        parent, new_ids = queue.popleft()
        for ref in references_into(parent):
            if ref.policy is None:
                raise UnhandledReference(
                    f"{ref.child.name}.{ref.column} -> {parent} has no retention policy "
                    "(add it to app/services/retention/purge.py POLICY)"
                )
            if ref.policy != DELETE:
                continue
            id_col = _id_column(ref.child)
            if id_col is None:
                if any(fk.column.table.name == ref.child.name
                       for t in md.tables.values() for fk in t.foreign_keys):
                    raise UnhandledReference(f"{ref.child.name} has no id column but is referenced")
                out.links.append((ref, parent))
                continue
            col = ref.child.c[ref.column]
            found: set[str] = set()
            for chunk in _chunks(new_ids):
                rows = await db.execute(
                    select(id_col).where(col.in_(chunk)).execution_options(include_rewound=True)
                )
                found.update(str(r[0]) for r in rows)
            fresh = found - out.ids[ref.child.name]
            if fresh:
                out.ids[ref.child.name] |= fresh
                queue.append((ref.child.name, fresh))
    return out


def _delete_order(tables: set[str]) -> list[str]:
    """Children before parents along NOT NULL references inside the set
    (nullable ones are cleared first, so they never block a delete)."""
    md = _metadata()
    deps: dict[str, set[str]] = {t: set() for t in tables}  # table -> parents it must precede
    for t in tables:
        for fk in md.tables[t].foreign_keys:
            parent = fk.column.table.name
            if parent in tables and parent != t and not fk.parent.nullable:
                deps[t].add(parent)
    order: list[str] = []
    remaining = set(tables)
    while remaining:
        # a table may go once no remaining table depends on it (needs it as parent)
        ready = sorted(t for t in remaining if not any(t in deps[o] for o in remaining if o != t))
        if not ready:  # a NOT NULL cycle: delete in a stable order and let the db decide
            ready = sorted(remaining)
        order.extend(ready)
        remaining -= set(ready)
    return order


async def execute(db: AsyncSession, plan: Closure) -> dict[str, int]:
    """Delete every row of ``plan``. Does not commit."""
    md = _metadata()
    tables = {t for t, ids in plan.ids.items() if ids}

    # 1. Rows that stay let go of rows that go.
    for parent in tables:
        for ref in references_into(parent):
            if ref.policy != NULL:
                continue
            col = ref.child.c[ref.column]
            for chunk in _chunks(plan.ids[parent]):
                await db.execute(update(ref.child).where(col.in_(chunk)).values({ref.column: None})
                                 .execution_options(include_rewound=True))

    # 2. Inside the closure, clear nullable references so cycles
    #    (completions.parent_id, queries.default_step_id <-> steps.query_id,
    #    artifacts.published_release_id <-> releases.artifact_id) never block.
    for t in tables:
        table = md.tables[t]
        id_col = _id_column(table)
        for fk in table.foreign_keys:
            if fk.parent.nullable and fk.column.table.name in tables:
                for chunk in _chunks(plan.ids[t]):
                    await db.execute(update(table).where(id_col.in_(chunk), fk.parent.isnot(None))
                                     .values({fk.parent.name: None}).execution_options(include_rewound=True))

    # 3. Link tables.
    for ref, parent in plan.links:
        col = ref.child.c[ref.column]
        for chunk in _chunks(plan.ids[parent]):
            await db.execute(delete(ref.child).where(col.in_(chunk)))

    # 4. The rows, children first.
    counts: dict[str, int] = {}
    for t in _delete_order(tables):
        table = md.tables[t]
        id_col = _id_column(table)
        for chunk in _chunks(plan.ids[t]):
            await db.execute(delete(table).where(id_col.in_(chunk)).execution_options(include_rewound=True))
        counts[t] = len(plan.ids[t])
    return counts


# -- files ---------------------------------------------------------------

def file_link_references() -> tuple[Reference, ...]:
    return references_into("files")


async def linked_file_ids(db: AsyncSession, plan: Closure) -> set[str]:
    """Files the rows of ``plan`` link to (candidates once they are gone)."""
    out: set[str] = set()
    file_cols: dict[str, list[str]] = defaultdict(list)
    for ref in file_link_references():
        file_cols[ref.child.name].append(ref.column)
    md = _metadata()
    # Rows with an id that go.
    for table_name, ids in plan.ids.items():
        if not ids or table_name not in file_cols:
            continue
        table = md.tables[table_name]
        id_col = _id_column(table)
        for column in file_cols[table_name]:
            for chunk in _chunks(ids):
                rows = await db.execute(select(table.c[column]).where(id_col.in_(chunk)))
                out.update(str(r[0]) for r in rows if r[0])
    # Link rows that go with their parent.
    for ref, parent in plan.links:
        for column in file_cols.get(ref.child.name, []):
            for chunk in _chunks(plan.ids[parent]):
                rows = await db.execute(select(ref.child.c[column]).where(ref.child.c[ref.column].in_(chunk)))
                out.update(str(r[0]) for r in rows if r[0])
    return out


async def unreferenced_files(db: AsyncSession, file_ids: Iterable[str]) -> list[str]:
    """Of ``file_ids``, those nothing links to any more."""
    remaining = {str(f) for f in file_ids}
    for ref in file_link_references():
        if not remaining:
            break
        col = ref.child.c[ref.column]
        for chunk in _chunks(remaining):
            rows = await db.execute(select(col).where(col.in_(chunk)))
            remaining -= {str(r[0]) for r in rows}
    return sorted(remaining)
