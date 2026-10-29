"""Edit Instruction Tool - Edits instructions during training mode exploration.

This tool allows the training mode agent to edit instructions that were created
in the current training session. All edits create new versions that are added
to the same draft build.
"""

import difflib
import re
from typing import AsyncIterator, Dict, Any, Optional, Tuple, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.implementations.create_instruction import clamp_evidence
from app.ai.tools.schemas.edit_instruction import EditInstructionInput, EditInstructionOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

# Minimum confidence to maintain for an instruction
MIN_CONFIDENCE_THRESHOLD = 0.7

# Valid categories
VALID_CATEGORIES = {"general", "code_gen", "visualization", "dashboard", "system"}

# load_mode values the tool may set. 'disabled' is deliberately absent: it reads
# as an off switch but is the weak one (the row still shows Active and is still
# returned by search) — `status: inactive` is the real off switch.
VALID_LOAD_MODES = {"always", "intelligent"}

# Tool vocabulary -> Instruction.status. "Inactive" is how the product labels a
# 'draft' instruction; every context loader filters on status == 'published'.
STATUS_TO_DB = {"active": "published", "inactive": "draft"}
DB_TO_STATUS = {v: k for k, v in STATUS_TO_DB.items()}

# Minimum length for the RESULTING instruction text after an edit. The schema
# no longer enforces min_length on `text` (an anchored replacement snippet may
# legitimately be short), so the invariant is checked on the final text here.
MIN_RESULT_TEXT_LENGTH = 20


def _closest_region(current: str, anchor: str, context: int = 60) -> Optional[str]:
    """Best-effort diagnostic for a failed anchor match: the region of the
    current text most similar to the anchor, so the model can retry with a
    snippet that actually exists (same idea as edit_artifact's near-miss
    reporting)."""
    try:
        sm = difflib.SequenceMatcher(None, current, anchor, autojunk=False)
        m = sm.find_longest_match(0, len(current), 0, len(anchor))
        if m.size < min(10, max(1, len(anchor) // 2)):
            return None
        start = max(0, m.a - context)
        end = min(len(current), m.a + m.size + context)
        return current[start:end]
    except Exception:
        return None


async def _base_status(db, instruction, pending_version) -> str:
    """The status this edit starts from — what the instruction is when the
    suggestion is reviewed, before any status change it asks for.

    The pending version in this build wins (an earlier edit this session may
    already have changed it). Otherwise it is the live row's status — but only
    for an instruction that is actually live. A create suggestion still awaiting
    review keeps its row at 'draft' until promotion, yet is meant to go live as
    'published'; reading the row there would stage the edit as Inactive.
    """
    if pending_version is not None:
        return pending_version.status or "published"
    status = getattr(instruction, "status", None) or "published"
    if status == "published":
        return status
    from app.services.instruction_service import InstructionService
    svc = InstructionService()
    if await svc._org_has_main_build(db, instruction.organization_id):
        # Main build exists: membership is what "live" means.
        return status if await svc._is_in_main_build(db, instruction) else "published"
    # No main build — a legacy org, or a new one whose first suggestions are
    # still in review. Membership proves nothing there; the current version
    # does: a pending create is staged 'published' over its 'draft' row, while
    # every person-made status change snapshots the row's own status.
    if instruction.current_version_id:
        from sqlalchemy import select
        from app.models.instruction_version import InstructionVersion
        v_status = (await db.execute(
            select(InstructionVersion.status)
            .where(InstructionVersion.id == str(instruction.current_version_id))
        )).scalar()
        if v_status:
            return v_status
    return status


async def _load_pending_version(db, build_id: Optional[str], instruction_id: str):
    """The version this build already stages for ``instruction_id``, if any.

    Edits are staged as versions and the live row is intentionally NOT mutated
    until the build is promoted, so within a single session the live row still
    holds the pre-edit text. ``add_to_build`` keeps exactly ONE version pointer
    per (build, instruction) — so a second edit that based itself on the live
    row would produce a version missing the first edit, and repointing the
    build to it would silently discard that first edit. Basing each edit on the
    pending version instead makes sequential edits accumulate.
    """
    if not build_id:
        return None
    from sqlalchemy import select
    from app.models.build_content import BuildContent
    from app.models.instruction_version import InstructionVersion

    result = await db.execute(
        select(InstructionVersion)
        .join(BuildContent, BuildContent.instruction_version_id == InstructionVersion.id)
        .where(
            BuildContent.build_id == build_id,
            BuildContent.instruction_id == instruction_id,
        )
    )
    return result.scalars().first()


async def _resolve_add_agents(db, instruction, pending_version, build, report,
                              requested, is_live: bool = True) -> dict:
    """The NEW scope an ``add_agents`` share stages on the version — the data
    sources and table references the instruction should carry once the named
    agents are added, with each new agent's same-named tables brought in:
    ``{"data_source_ids": [...], "references": [...], "added": [names]}``.
    Returns ``{"reject": (code, message)}`` when it cannot be honoured as
    asked — never a partial share.

    Only agents attached to the conversation can be added, so a name never
    reaches an agent nobody mentioned (unlike ``table_names``, which matches
    tables org-wide). The base scope is the LIVE attachment with whatever THIS
    build already staged (its base->pending delta) applied on top — so two
    edits in one session accumulate, while a carry-over snapshot of main (which
    may be stale) contributes nothing. A scope a reviewer already rejected in
    this build is not re-proposed.
    """
    from sqlalchemy import select, func
    from app.models.data_source import DataSource
    from app.models.datasource_table import DataSourceTable
    from app.models.report_data_source_association import report_data_source_association as rdsa
    from app.services.field_changes import scope_change_key, scope_delta, merge_refs, _clean_refs, _ref_ident

    # Start from the live attachment, then apply what this build already staged
    # for the instruction (its base->pending scope delta). A carry-over row
    # (no real edit) has a zero delta, so a stale main snapshot never leaks in.
    current_ds = {str(ds.id) for ds in (instruction.data_sources or [])}
    current_refs = _clean_refs([
        {"object_type": r.object_type, "object_id": str(r.object_id),
         "column_name": r.column_name, "relation_type": getattr(r, "relation_type", None),
         "display_text": r.display_text}
        for r in (instruction.references or [])
    ])
    if pending_version is not None and build is not None:
        from app.models.build_content import BuildContent
        from app.models.instruction_version import InstructionVersion as _IV
        row = (await db.execute(
            select(BuildContent.base_version_id, BuildContent.is_change).where(
                BuildContent.build_id == str(build.id),
                BuildContent.instruction_id == str(instruction.id))
        )).first()
        if row and row[1] and row[0]:  # a real staged change with a base
            bver = await db.get(_IV, str(row[0]))
            base_ds = bver.data_source_ids if bver is not None else None
            base_refs = bver.references_json if bver is not None else None
            a_ds, r_ds, a_refs, r_refs = scope_delta(
                base_ds, base_refs, pending_version.data_source_ids, pending_version.references_json)
            current_ds = (current_ds | set(a_ds)) - set(r_ds)
            rem = {_ref_ident(r) for r in r_refs}
            current_refs = [r for r in merge_refs(current_refs, a_refs) if _ref_ident(r) not in rem]
    if not current_ds:
        return {"reject": ("global_instruction",
                           "This instruction is global — every agent already uses it.")}
    if not is_live:
        # A share is reviewed against the instruction's live agents; an
        # instruction nobody accepted yet has none, so the share could never
        # be shown.
        return {"reject": ("not_live",
                           "This instruction is not published yet. Share it with another "
                           "agent after a reviewer accepts it.")}

    convo = []
    report_id = getattr(report, "id", None)
    if report_id:
        convo = (await db.execute(
            select(DataSource.id, DataSource.name)
            .join(rdsa, rdsa.c.data_source_id == DataSource.id)
            .where(rdsa.c.report_id == str(report_id), DataSource.deleted_at.is_(None))
        )).all()
    by_id = {str(did): (str(did), name) for did, name in convo}
    by_name: dict = {}
    for did, name in convo:
        by_name.setdefault((name or "").strip().lower(), []).append((str(did), name))
    picked, unknown, ambiguous = {}, [], []
    for raw in requested:
        key = str(raw).strip()
        if key in by_id:
            hits = [by_id[key]]
        else:
            hits = by_name.get(key.lower(), [])
        if not hits:
            unknown.append(key)
        elif len(hits) > 1:
            ambiguous.append(key)
        else:
            picked[hits[0][0]] = hits[0][1]
    if ambiguous:
        return {"reject": ("ambiguous_agent",
                           f"More than one agent in this conversation is named "
                           f"{', '.join(ambiguous)}; name it by id instead.")}
    if unknown:
        here = ", ".join(sorted(n for _d, n in convo)) or "none"
        return {"reject": ("agent_not_in_conversation",
                           f"Not attached to this conversation: {', '.join(unknown)}. "
                           f"Agents in this conversation: {here}.")}
    new = {d: n for d, n in picked.items() if d not in current_ds}
    if not new:
        return {"reject": ("no_change",
                           f"The instruction is already shared with {', '.join(sorted(picked.values()))}.")}

    table_ids = sorted({
        str(r["object_id"]) for r in current_refs
        if r.get("object_type") == "datasource_table" and r.get("object_id")
    })
    names = set()
    if table_ids:
        names = {n.lower() for (n,) in (await db.execute(
            select(DataSourceTable.name).where(DataSourceTable.id.in_(table_ids))
        )).all() if n}
    brought, missing = [], []
    if names:
        found: dict = {}
        for t in (await db.execute(
            select(DataSourceTable).where(
                DataSourceTable.datasource_id.in_(sorted(new)),
                DataSourceTable.is_active.is_(True),
                func.lower(DataSourceTable.name).in_(sorted(names)),
            )
        )).scalars().all():
            found.setdefault(str(t.datasource_id), []).append(t)
        for did, dname in sorted(new.items(), key=lambda kv: kv[1] or ""):
            if not found.get(did):
                missing.append(dname)
                continue
            brought += [{"object_type": "datasource_table", "object_id": str(t.id),
                         "column_name": None, "relation_type": None, "display_text": t.name}
                        for t in found[did]]
    if missing:
        return {"reject": ("no_matching_tables",
                           f"{', '.join(missing)} has none of this instruction's tables "
                           f"({', '.join(sorted(names))}), so it was not shared.")}

    new_ds = sorted(current_ds | set(new))
    new_refs = current_refs + brought
    # A scope a reviewer already rejected in this build is not re-proposed —
    # the review key binds the exact proposed scope (agents + tables).
    rejected_keys = {
        r.get("key") for r in (getattr(build, "rejected_hunks", None) or [])
        if r.get("instruction_id") == str(instruction.id) and r.get("action") == "reject"
    }
    if scope_change_key(new_ds, new_refs) in rejected_keys:
        return {"reject": ("share_rejected",
                           f"A reviewer already rejected sharing this instruction with "
                           f"{', '.join(sorted(new.values()))}. Do not propose it again.")}
    return {"data_source_ids": new_ds, "references": new_refs, "added": sorted(new.values())}


def _apply_anchor_edit(
    current: str, old_text: str, new_snippet: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Replace ``old_text`` with ``new_snippet`` inside ``current``.

    Matching is exact-first, then whitespace-normalized (instructions are
    human prose, so runs of spaces/newlines must not fail the match).

    Returns (result_text, error_code, error_detail); exactly one of
    result_text / error_code is set. error_code is 'anchor_not_found' or
    'ambiguous_anchor'.
    """
    # Exact substring match
    count = current.count(old_text)
    if count == 1:
        return current.replace(old_text, new_snippet, 1), None, None
    if count > 1:
        return None, "ambiguous_anchor", (
            f"old_text matches {count} locations in the instruction text. "
            "Provide a longer, unique snippet."
        )

    # Whitespace-normalized fallback: whitespace runs in the anchor match any
    # whitespace run in the text.
    tokens = old_text.split()
    if tokens:
        pattern = r"\s+".join(re.escape(tok) for tok in tokens)
        matches = list(re.finditer(pattern, current))
        if len(matches) == 1:
            m = matches[0]
            return current[: m.start()] + new_snippet + current[m.end():], None, None
        if len(matches) > 1:
            return None, "ambiguous_anchor", (
                f"old_text matches {len(matches)} locations in the instruction text "
                "(after whitespace normalization). Provide a longer, unique snippet."
            )

    detail = "old_text was not found in the current instruction text."
    closest = _closest_region(current, old_text)
    if closest:
        detail += f" Closest matching region: \"…{closest}…\""
    return None, "anchor_not_found", detail


class EditInstructionTool(Tool):
    """Edit instruction tool - edits existing instructions during training mode.

    This tool is available only in training mode. It edits instructions that
    belong to the organization. Edits create new versions that are tracked
    in the training draft build.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_instruction",
            description=(
                "ACTION: Edit an existing instruction. "
                "Use when you need to correct mistakes, improve clarity, update confidence after "
                "user confirmation, or refine table associations.\n\n"
                "TEXT EDITS ARE ANCHORED. `text` always needs `old_text`: a short unique "
                "snippet of the CURRENT instruction text, which `text` replaces. It must be "
                "real text — \"\" is not an anchor, and omitting it is not either; both are "
                "REJECTED (rejected_reason='anchor_required'). To ADD a rule at the end, "
                "anchor the last sentence and repeat it verbatim at the start of `text` "
                "followed by your addition.\n\n"
                "CHANGE ONLY WHAT WAS ASKED. Encode the rule the user's request or your cited "
                "evidence establishes, and nothing else. Do not restate untouched sentences, "
                "propagate the change through the rest of the document for consistency, or add "
                "adjacent rules and tool-usage guidance nobody requested — whether the rest "
                "should follow suit is the reviewer's call, not yours. Leave metadata "
                "(category, load_mode, confidence, status) alone unless the request is about it. When "
                "reverting, remove exactly what was added — not the sentence that contained "
                "it.\n\n"
                "SEVERAL CHANGES = SEVERAL CALLS. One call edits one place. If a request "
                "touches three sentences, make three edit_instruction calls, each anchored on "
                "its own snippet — sequentially is fine, they need not be in one turn, and the "
                "observation of each returns the resulting text for the next one to anchor on. "
                "That is the CORRECT shape, not a workaround: never widen a single edit, and "
                "never reach for a rewrite, because a change touches more than one place.\n\n"
                "REWRITING EVERYTHING is a separate, deliberate act: `replace_entire_text: true`, "
                "and only when the user explicitly asked to rewrite or start over — never "
                "because several places need changing, and never to work around a failed "
                "anchor. Rejected in knowledge mode.\n\n"
                "ANCHOR ON TEXT YOU HAVE READ. Anchors must match the instruction's CURRENT "
                "text exactly. Call read_instruction first if you have not seen it this "
                "session; after an edit of your own, anchor on the `new_text` that edit "
                "returned, not on what the instruction said before it.\n\n"
                "CHECK WHAT IS ALREADY PROPOSED. read_instruction also returns "
                "`pending_changes` — suggestions awaiting review, with who proposed each and "
                "when. Those are NOT part of the live text: never anchor on them (the anchor "
                "will not match) and do not assume they landed. If one already makes the "
                "change you were about to make, say so and ask whether to replace it rather "
                "than stacking a second suggestion; if the user asks you to undo something "
                "that is still only pending, the answer is that it was never applied — do not "
                "edit the live text to remove it.\n\n"
                "TURNING AN INSTRUCTION OFF: `status: \"inactive\"` (no text needed) takes it "
                "out of use for every agent while keeping its text; `status: \"active\"` turns it "
                "back on. Use it when the user asks to stop using, disable or turn off an "
                "instruction, or it is confirmed wrong as a whole. Do NOT delete its text to "
                "achieve the same, and do not use load_mode for it. Not available in knowledge "
                "mode.\n\n"
                "SCOPING — table_names: sets WHICH TABLES the instruction is about. A NON-EMPTY "
                "list changes the table references ONLY and never which agents the instruction is "
                "attached to (use add_agents for that). OMIT the field entirely to leave tables "
                "unchanged. The ONE EXCEPTION is an empty list []: it is the explicit 'make this "
                "instruction global' gesture — it drops every agent so the rule applies to ALL of "
                "them (an org-level change a reviewer must approve). So pass [] ONLY when you truly "
                "mean global; to merely clear a stale table, pass the tables you DO want, not []. "
                "Listing every table inspected is wrong — it scopes the instruction and may "
                "prevent it from loading in unrelated queries.\n\n"
                "SHARING WITH ANOTHER AGENT — add_agents: to make an existing instruction apply to "
                "another agent of this conversation too, pass that agent's name in add_agents. "
                "This is the ONLY way to change which agents an instruction is attached to; "
                "table_names never does.\n\n"
                "EVIDENCE: always set `evidence` — ONE short sentence (under 150 characters) "
                "naming the source and the fact. Reviewers see it next to the suggested change; "
                "no preamble."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditInstructionInput.model_json_schema(),
            output_schema=EditInstructionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=["manage_instructions"],
            tags=["training", "instruction", "semantic-learning"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "confidence": 0.95,
                        "evidence": "User confirmed: status 1=active, 2=inactive, 3=banned."
                    },
                    "description": "Update confidence only — omit table_names to keep existing scope."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "old_text": "Exclude cancelled orders from revenue.",
                        "text": "Exclude cancelled orders from revenue. Refunded orders are excluded too.",
                        "evidence": "User clarified: refunds never count toward revenue."
                    },
                    "description": "Add a related learning by anchoring the sentence it belongs after and repeating it verbatim before the addition."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "old_text": "exclude orders with status='cancelled'",
                        "text": "exclude orders with status='cancelled' or status='refunded'",
                        "evidence": "User corrected: refunded orders must be excluded too."
                    },
                    "description": "Surgical correction — old_text anchors the exact snippet to replace."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "old_text": "find relevant files by topic",
                        "text": "find relevant PDF files by topic",
                        "evidence": "User asked to handle only PDFs."
                    },
                    "description": "Narrow one rule to PDFs. A request that touches several sentences is several anchored calls like this — NOT a rewrite, and untouched sentences stay untouched."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "table_names": []
                    },
                    "description": "Make it global — an empty list drops every agent so the instruction applies everywhere (reviewer-approved, org-level)."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "category": "code_gen",
                        "load_mode": "always"
                    },
                    "description": "Change category and load mode for a critical rule."
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "status": "inactive",
                        "evidence": "User asked to stop applying the legacy fiscal-calendar rule."
                    },
                    "description": "Turn an instruction off without touching its text."
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditInstructionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditInstructionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        """Execute edit_instruction - updates instruction in training session's draft build."""

        try:
            data = EditInstructionInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Invalid input: {str(e)}",
                    "code": "INVALID_INPUT"
                }
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "instruction_id": data.instruction_id,
                "updating_fields": [k for k, v in tool_input.items() if v is not None and k != "instruction_id"],
            }
        )


        # Validate confidence threshold if provided
        if data.confidence is not None and data.confidence < MIN_CONFIDENCE_THRESHOLD:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=f"Confidence {data.confidence} is below minimum threshold {MIN_CONFIDENCE_THRESHOLD}.",
                        rejected_reason="low_confidence"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edit rejected: confidence {data.confidence} < {MIN_CONFIDENCE_THRESHOLD}",
                        "artifacts": [],
                    },
                }
            )
            return

        # Validate load_mode if provided. Unknown values (notably 'disabled')
        # used to be coerced to 'intelligent' — a request to turn an
        # instruction off silently widened when it loads.
        if data.load_mode is not None and data.load_mode not in VALID_LOAD_MODES:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=(
                            f"Invalid load_mode '{data.load_mode}'. Must be one of: "
                            f"{', '.join(sorted(VALID_LOAD_MODES))}. To take the instruction "
                            "out of use, set status: 'inactive' instead."
                        ),
                        rejected_reason="invalid_load_mode",
                    ).model_dump(),
                    "observation": {
                        "summary": (
                            f"Edit rejected: invalid load_mode '{data.load_mode}'. "
                            "Use status: 'inactive' to turn an instruction off."
                        ),
                        "artifacts": [],
                    },
                }
            )
            return

        # Validate category if provided
        if data.category is not None and data.category not in VALID_CATEGORIES:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=f"Invalid category '{data.category}'. Must be one of: {', '.join(VALID_CATEGORIES)}",
                        rejected_reason="invalid_category"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edit rejected: invalid category '{data.category}'",
                        "artifacts": [],
                    },
                }
            )
            return

        # Get required context from runtime
        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        training_build_id = runtime_ctx.get("training_build_id")
        agent_execution_id = runtime_ctx.get("agent_execution_id")
        mode = runtime_ctx.get("mode")
        report = runtime_ctx.get("report")

        # In knowledge-harness / post-analysis mode, restrict table resolution
        # to data sources actually attached to the current report (parity with
        # create_instruction). Training mode is broader — user is intentionally
        # curating the org — so we keep it org-scoped there.
        allowed_data_source_ids = None
        if mode == "knowledge" and report is not None:
            try:
                allowed_data_source_ids = {
                    str(ds.id) for ds in (report.data_sources or [])
                }
            except Exception:
                allowed_data_source_ids = set()

        # Turning an instruction on or off changes behaviour for every agent
        # that loads it. The knowledge harness edits autonomously, so — like
        # replace_entire_text — it must not be able to retire curated content
        # (or revive what a person switched off). Training mode, where a user
        # is curating deliberately, may.
        if data.status is not None and mode == "knowledge":
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=(
                            "Changing an instruction's status is not allowed in knowledge "
                            "mode. If it should be turned on or off, suggest that to the user."
                        ),
                        rejected_reason="status_change_not_allowed",
                    ).model_dump(),
                    "observation": {
                        "summary": (
                            "Edit rejected: status changes are not allowed in knowledge "
                            "mode. Do not retry — suggest the change to the user instead."
                        ),
                        "artifacts": [],
                    },
                }
            )
            return

        # Lazy build creation: the harness no longer pre-seeds a draft.
        # The first create/edit in a session lazily creates the build below
        # (in the add_to_build path) and writes the id back into runtime_ctx.
        if not all([db, organization]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization)",
                    "code": "MISSING_CONTEXT"
                }
            )
            return

        try:
            from sqlalchemy import select, or_, func
            from sqlalchemy.orm import selectinload
            from app.models.instruction import Instruction
            from app.models.datasource_table import DataSourceTable
            from app.models.data_source import DataSource
            from app.services.build_service import BuildService
            from app.services.instruction_version_service import InstructionVersionService
            from app.schemas.instruction_reference_schema import InstructionReferenceCreate

            build_service = BuildService()
            version_service = InstructionVersionService()

            # Fetch the instruction (read-only — we do NOT mutate the live row).
            # Edits are staged as a new InstructionVersion attached to the draft
            # build. Live row is updated only when the build is promoted.
            stmt = (
                select(Instruction)
                .options(
                    selectinload(Instruction.data_sources),
                    selectinload(Instruction.labels),
                    selectinload(Instruction.references),
                )
                .where(
                    Instruction.id == data.instruction_id,
                    Instruction.organization_id == str(organization.id)
                )
                # The agent's session is long-lived; reload the row and its
                # agents/references instead of trusting an earlier copy.
                .execution_options(populate_existing=True)
            )
            result = await db.execute(stmt)
            instruction = result.scalar_one_or_none()

            if not instruction:
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": EditInstructionOutput(
                            success=False,
                            instruction_id=data.instruction_id,
                            message=f"Instruction '{data.instruction_id}' not found",
                            rejected_reason="not_found"
                        ).model_dump(),
                        "observation": {
                            "summary": f"Edit failed: instruction '{data.instruction_id}' not found",
                            "artifacts": [],
                        },
                    }
                )
                return

            # Cross-agent authority gate (knowledge/post-analysis mode): a shared
            # instruction is a single row loaded by every attached agent. Editing
            # one from a session scoped to a subset of its agents would stage a
            # change that reaches agents outside this session — the
            # instruction-sharing escalation. Refuse when the instruction is
            # attached to any data source outside the session's scope. Training
            # mode is deliberately broader (explicit org-wide curation), and a
            # GLOBAL instruction (no attached agents) passes this gate — its
            # blast radius is handled by the human publish gate, which requires
            # org-level authority to promote. Ported from PR #732.
            if allowed_data_source_ids is not None:
                attached_ds_ids = {str(ds.id) for ds in (instruction.data_sources or [])}
                out_of_scope = attached_ds_ids - set(allowed_data_source_ids)
                if out_of_scope:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=data.instruction_id,
                                title=getattr(instruction, "title", None),
                                message=(
                                    "Cannot edit this instruction: it is shared with "
                                    "agent(s) outside this session. Editing it would "
                                    "change behaviour for agents not in scope."
                                ),
                                rejected_reason="out_of_scope",
                            ).model_dump(),
                            "observation": {
                                "summary": (
                                    "Edit rejected: instruction shared with out-of-scope "
                                    "agents. Do not retry — suggest the change to the "
                                    "user instead."
                                ),
                                "artifacts": [],
                            },
                        }
                    )
                    return

            # === Resolve the base this edit applies to ===
            # Prefer the version already staged in this build over the live row:
            # the live row is not updated until promotion, so a second edit in
            # the same session would otherwise re-read pre-edit text and drop
            # the first edit (see _load_pending_version).
            build = None
            if training_build_id:
                build = await build_service.get_build(db, training_build_id)
            pending_version = None
            if build is not None and build.can_be_edited:
                pending_version = await _load_pending_version(
                    db, str(build.id), str(instruction.id)
                )
            base_text = (
                pending_version.text if pending_version is not None else instruction.text
            )

            previous_text = base_text if data.text is not None else None

            # === Compute the resulting text (anchor semantics) ===
            # old_text non-empty  -> search/replace within the current text
            # old_text == ""      -> rejected (not an anchor)
            # old_text omitted    -> rejected, unless replace_entire_text=true
            #
            # The anchor is mandatory by design. Every mainstream edit tool
            # (Claude Code's Edit, aider's SEARCH/REPLACE, unified diffs) makes
            # the old side required and puts whole-file replacement behind a
            # SEPARATELY NAMED act — so a surgical edit is the only thing the
            # edit tool can express, and destroying content takes a deliberate
            # decision rather than one fewer argument. This tool used to invert
            # that: omitting old_text — the call with the FEWEST arguments —
            # replaced the whole instruction. Asked to add one PDF-only rule, a
            # model took that path and rewrote every bullet plus category,
            # load_mode and confidence, because "one call that always applies"
            # beat "several anchored calls that can miss".
            new_text = base_text
            if data.text is not None:
                current_text = base_text or ""
                if data.old_text is None and not data.replace_entire_text:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=(
                                    "Edit rejected: `text` needs an anchor. Pass `old_text` with "
                                    "an exact snippet of the current text to replace it, or "
                                    "`old_text: \"\"` to append your text as a new paragraph. "
                                    "Make several small anchored edits rather than one large "
                                    "one. Only if the user explicitly asked to rewrite the whole "
                                    "instruction, re-send with `replace_entire_text: true`. "
                                    f"Current instruction text: \"{current_text}\""
                                ),
                                rejected_reason="anchor_required",
                            ).model_dump(),
                            "observation": {
                                "summary": (
                                    "Edit rejected: `text` without `old_text`. Retry with "
                                    "old_text (exact snippet to replace) or old_text: \"\" "
                                    "(append). Change only what was asked — several small "
                                    "anchored edits, not one rewrite."
                                ),
                                "current_text": current_text,
                                "artifacts": [],
                            },
                        }
                    )
                    return
                if data.old_text is None:
                    if mode == "knowledge":
                        yield ToolEndEvent(
                            type="tool.end",
                            payload={
                                "output": EditInstructionOutput(
                                    success=False,
                                    instruction_id=str(instruction.id),
                                    title=getattr(instruction, "title", None),
                                    message=(
                                        "replace_entire_text is not allowed in knowledge mode — "
                                        "the harness edits autonomously, so it must not be able "
                                        "to discard curated content. Pass old_text with an exact "
                                        "snippet of the current text to replace it, or "
                                        "old_text: \"\" to append your text as a new paragraph. "
                                        f"Current instruction text: \"{current_text}\""
                                    ),
                                    rejected_reason="full_replace_not_allowed",
                                ).model_dump(),
                                "observation": {
                                    "summary": (
                                        "Edit rejected: full rewrite not allowed in knowledge mode. "
                                        "Retry with old_text (exact snippet to replace) or "
                                        "old_text: \"\" (append)."
                                    ),
                                    "current_text": current_text,
                                    "artifacts": [],
                                },
                            }
                        )
                        return
                    new_text = data.text
                elif data.old_text == "":
                    # `old_text: ""` used to mean "append". That was the last
                    # sentinel overload on this parameter — one field meaning
                    # three different operations depending on whether it was a
                    # real string, empty, or absent. Standard edit tools have no
                    # such convention: the anchor is always real text, and you
                    # add at the end by anchoring the last sentence and
                    # including it in the replacement. One rule is easier to get
                    # right than three, and an anchored append states WHERE the
                    # addition goes instead of always landing at the bottom.
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=(
                                    "Edit rejected: `old_text` must be real text from the "
                                    "instruction — \"\" is not an anchor. To ADD something at "
                                    "the end, anchor the last sentence and repeat it verbatim "
                                    "at the start of `text`, followed by your addition. "
                                    f"Current instruction text: \"{current_text}\""
                                ),
                                rejected_reason="anchor_required",
                            ).model_dump(),
                            "observation": {
                                "summary": (
                                    "Edit rejected: old_text: \"\" is not an anchor. Anchor on "
                                    "an exact snippet of the current text; to append, anchor "
                                    "the last sentence and repeat it before your addition."
                                ),
                                "current_text": current_text,
                                "artifacts": [],
                            },
                        }
                    )
                    return
                else:
                    applied, err_code, err_detail = _apply_anchor_edit(
                        current_text, data.old_text, data.text
                    )
                    if err_code == "anchor_not_found":
                        # The classic miss: anchoring on wording from an edit
                        # staged in an EARLIER turn. That edit is pending review
                        # in another build, so the current text never contains
                        # it — and models tend to trust their conversation
                        # memory over the text echoed back. Say so explicitly.
                        err_detail += (
                            " If you anchored on wording from an edit you made in a "
                            "previous turn: that edit is still pending review and is "
                            "NOT part of the current text. Anchor on the current text "
                            "exactly as quoted below and apply only this turn's change."
                        )
                    if err_code:
                        yield ToolEndEvent(
                            type="tool.end",
                            payload={
                                "output": EditInstructionOutput(
                                    success=False,
                                    instruction_id=str(instruction.id),
                                    title=getattr(instruction, "title", None),
                                    message=(
                                        f"Edit rejected: {err_detail} "
                                        "Current instruction text: "
                                        f"\"{current_text}\""
                                    ),
                                    rejected_reason=err_code,
                                ).model_dump(),
                                "observation": {
                                    "summary": f"Edit rejected ({err_code}): {err_detail}",
                                    "current_text": current_text,
                                    "artifacts": [],
                                },
                            }
                        )
                        return
                    new_text = applied

                if len((new_text or "").strip()) < MIN_RESULT_TEXT_LENGTH:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=(
                                    f"Edit rejected: resulting instruction text would be shorter "
                                    f"than {MIN_RESULT_TEXT_LENGTH} characters."
                                ),
                                rejected_reason="result_too_short",
                            ).model_dump(),
                            "observation": {
                                "summary": "Edit rejected: resulting text too short",
                                "artifacts": [],
                            },
                        }
                    )
                    return

                # NO GENERALITY GATE ON EDITS.
                #
                # The gate used to run here on the RESULTING text — the whole
                # instruction, not the change. That judged the model on prose it
                # did not write and was not asked to touch: an instruction whose
                # inherited text contains anything the critic dislikes fails
                # every edit forever, however small and however general the edit
                # itself is. Onboarding (data_source_service, which writes
                # through the service and so never faced the critic) routinely
                # produces exactly that — "no files were indexed at
                # schema-review time", "the schema lists no tables" — and those
                # read as observed values.
                #
                # Worse, the rejection ARGUED FOR A REWRITE: "restate the edit
                # as the general rule ... or leave the instruction unchanged"
                # tells a model the document is the problem. Watched live, that
                # is exactly what it concluded — it stopped making the one-line
                # change it was asked for and rewrote everything to purge the
                # offending inherited lines.
                #
                # The veto is also redundant. Every AI-authored edit is staged
                # as a suggestion a human accepts or rejects hunk by hunk, so
                # there is already a gate, and it is one that can tell "this
                # sentence was here before" from "the model just wrote this".
                # create_instruction keeps its gate: there the model authored
                # every word, so judging the whole text is judging its own work.

            # Start from the current live row state, overlay only the fields
            # the caller wants to change. These values become the new version.
            # Unchanged fields carry over from the same base as the text (the
            # pending version when this build already stages one), so a second
            # edit doesn't revert metadata set by the first.
            base_title = (
                pending_version.title if pending_version is not None else instruction.title
            )
            base_load_mode = (
                pending_version.load_mode if pending_version is not None else instruction.load_mode
            )
            new_title = data.title if data.title is not None else base_title
            new_category = data.category if data.category is not None else instruction.category
            if data.load_mode is not None:
                new_load_mode = data.load_mode  # validated above
            else:
                new_load_mode = base_load_mode or "always"

            # === Status ===
            # Staged on the version, applied when a reviewer accepts. Without a
            # requested change the version carries the CURRENT status, so a text
            # edit to an Inactive instruction can never switch it back on.
            base_status = await _base_status(db, instruction, pending_version)
            new_status = STATUS_TO_DB[data.status] if data.status else base_status
            if data.status is not None:
                status_error = None
                if base_status not in DB_TO_STATUS:
                    status_error = (
                        "status_change_not_allowed",
                        f"This instruction is {base_status}; only active and inactive "
                        "instructions can be switched on or off.",
                    )
                elif new_status == base_status and not any(
                    v is not None for v in (
                        data.text, data.title, data.category, data.confidence,
                        data.load_mode, data.table_names, data.add_agents or None,
                    )
                ):
                    status_error = (
                        "no_change",
                        f"The instruction is already {data.status}.",
                    )
                if status_error:
                    code, msg = status_error
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=f"Edit rejected: {msg}",
                                rejected_reason=code,
                            ).model_dump(),
                            "observation": {
                                "summary": f"Edit rejected ({code}): {msg}",
                                "artifacts": [],
                            },
                        }
                    )
                    return

            # === Sharing with more agents (add_agents) ===
            shared = None
            if data.add_agents:  # an empty list asks for nothing
                if mode == "knowledge":
                    # Like status and full rewrites: the harness edits on its own,
                    # and sharing a rule reaches agents nobody asked about.
                    shared = {"reject": ("share_not_allowed",
                                         "Sharing an instruction with another agent is only "
                                         "available in training mode. Suggest it to the user instead.")}
                elif data.table_names is not None:
                    shared = {"reject": ("conflicting_scope",
                                         "Pass add_agents or table_names, not both.")}
                else:
                    # Live = served by the main build. An org that predates
                    # main builds serves its rows directly (see _main_text_of),
                    # so there a published row is live; a draft staged by the
                    # create tool in an org with no main build yet is not.
                    is_live = (await build_service._current_main_version_id(
                        db, str(instruction.id), str(organization.id),
                    )) is not None or (
                        not await build_service.get_main_build(db, str(organization.id))
                        and instruction.status == "published"
                    )
                    shared = await _resolve_add_agents(
                        db, instruction, pending_version, build, report,
                        data.add_agents, is_live=is_live,
                    )
                if "reject" in shared:
                    code, msg = shared["reject"]
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": EditInstructionOutput(
                                success=False,
                                instruction_id=str(instruction.id),
                                title=getattr(instruction, "title", None),
                                message=f"Edit rejected: {msg}",
                                rejected_reason=code,
                            ).model_dump(),
                            "observation": {
                                "summary": f"Edit rejected ({code}): {msg}",
                                "artifacts": [],
                            },
                        }
                    )
                    return

            # Scope has two independent axes, and this tool keeps them apart:
            #   - WHICH AGENTS (data_source_ids) — changed by add_agents (a
            #     share), or emptied to go global;
            #   - WHICH TABLES (references_json) — changed by table_names.
            # A table_names edit therefore touches references ONLY and leaves
            # the agent attachment as it is: matching a table that happens to
            # live under agent A must not silently detach agent B. The one
            # exception is table_names=[], the documented "make global" gesture,
            # which clears the agents (reviewed, and org-level authority applies
            # at accept/publish). add_agents and table_names are mutually
            # exclusive (conflicting_scope above).
            matched_table_names = []
            # The agent scope carried forward unchanged by a table_names edit:
            # the pending draft's if there is one, else the instruction's live
            # attachment.
            current_ds_ids = (
                (list(pending_version.data_source_ids) if pending_version is not None
                 and pending_version.data_source_ids is not None
                 else [ds.id for ds in (instruction.data_sources or [])]) or None
            )
            if shared is not None and "data_source_ids" in shared:
                new_data_source_ids = shared["data_source_ids"] or None
                new_references_json = shared["references"] or None
            elif data.table_names is not None:
                if not data.table_names:
                    # Explicit "make global": drop every agent (and, with no
                    # agents, the agent-specific table references).
                    new_data_source_ids = []
                    new_references_json = None
                else:
                    # Resolve the requested table list to references only; the
                    # agent attachment is carried forward untouched. Match tables
                    # WITHIN the instruction's own agents, so a reference can only
                    # point at a table the instruction can actually use — never a
                    # table under an agent it is not attached to. A global
                    # instruction (no agents) matches across the report's agents,
                    # since it applies to all of them.
                    resolved_refs = []
                    conditions = []
                    for name in data.table_names:
                        name_lower = name.lower()
                        if '.' in name:
                            conditions.append(func.lower(DataSourceTable.name) == name_lower)
                        else:
                            conditions.append(func.lower(DataSourceTable.name) == name_lower)
                            conditions.append(func.lower(DataSourceTable.name).like(f'%.{name_lower}'))
                    if current_ds_ids:
                        match_ds = {str(d) for d in current_ds_ids}
                        if allowed_data_source_ids is not None:
                            match_ds &= {str(d) for d in allowed_data_source_ids}
                    elif allowed_data_source_ids is not None:
                        match_ds = {str(d) for d in allowed_data_source_ids}
                    else:
                        match_ds = None  # no restriction (admin, global, no report scope)
                    if conditions:
                        where_clauses = [
                            DataSource.organization_id == str(organization.id),
                            or_(*conditions),
                        ]
                        if match_ds is not None:
                            # Empty set → nothing to match (global in a report with
                            # no accessible agents, or no overlap).
                            where_clauses.append(DataSource.id.in_(list(match_ds)))
                        table_stmt = (
                            select(DataSourceTable)
                            .join(DataSource, DataSourceTable.datasource_id == DataSource.id)
                            .where(*where_clauses)
                        )
                        table_result = await db.execute(table_stmt)
                        tables = table_result.scalars().all()
                        for table in tables:
                            resolved_refs.append({
                                "object_type": "datasource_table",
                                "object_id": str(table.id),
                                "column_name": None,
                                "display_text": table.name,
                            })
                            matched_table_names.append(table.name)
                    new_data_source_ids = current_ds_ids
                    new_references_json = resolved_refs or None
            elif pending_version is not None:
                new_data_source_ids = pending_version.data_source_ids or None
                new_references_json = pending_version.references_json or None
            else:
                new_data_source_ids = [ds.id for ds in (instruction.data_sources or [])] or None
                new_references_json = [
                    {
                        "object_type": ref.object_type,
                        "object_id": ref.object_id,
                        "column_name": ref.column_name,
                        "display_text": ref.display_text,
                    }
                    for ref in (instruction.references or [])
                ] or None
            label_ids = [label.id for label in (instruction.labels or [])] or None
            category_ids = None
            if new_category:
                category_ids = new_category if isinstance(new_category, list) else [new_category]

            version_number = None
            try:
                version = await version_service.create_version_from_data(
                    db=db,
                    instruction_id=str(instruction.id),
                    text=new_text,
                    title=new_title,
                    description=instruction.description,
                    structured_data=instruction.structured_data,
                    formatted_content=instruction.formatted_content,
                    # AI-suggested edit: the requested status, else the
                    # instruction's current one — 'published' for a suggestion
                    # not yet live, so promote_build still flips that row on
                    # approval (see _base_status). The live row is untouched
                    # until a reviewer accepts.
                    status=new_status,
                    load_mode=new_load_mode,
                    references_json=new_references_json,
                    data_source_ids=new_data_source_ids,
                    label_ids=label_ids,
                    category_ids=category_ids,
                    user_id=user.id if user else None,
                    evidence=clamp_evidence(data.evidence),
                )
                version_number = version.version_number

                # `build` was resolved above (it determines the edit's base).
                if not build or not build.can_be_edited:
                    # No usable draft yet — create one now and write the id
                    # back so subsequent harness tool calls share it.
                    build = await build_service.get_or_create_draft_build(
                        db=db,
                        org_id=str(organization.id),
                        source='ai',
                        user_id=str(user.id) if user else None,
                        agent_execution_id=agent_execution_id,
                    )
                    runtime_ctx["training_build_id"] = str(build.id)
                    training_build_id = str(build.id)
                if build and build.can_be_edited:
                    await build_service.add_to_build(
                        db, build.id, str(instruction.id), version.id
                    )

                await db.commit()
                logger.info(
                    f"Staged edit as version {version.id} (v{version_number}) for instruction {instruction.id} in build {training_build_id}"
                )
            except Exception as e:
                # Staging is the whole point of the edit — if it fails, say so
                # rather than reporting success (and, for a share, telling the
                # user it is waiting for review when nothing was staged).
                logger.warning(f"Failed to stage edit for instruction {instruction.id}: {e}")
                # Roll the aborted transaction back: a failed staging write (e.g.
                # a race on the build-content unique index) leaves the session in
                # a pending-rollback state, and the NEXT tool call in this run
                # would then blow up with PendingRollbackError on its first query.
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": EditInstructionOutput(
                            success=False,
                            instruction_id=str(instruction.id),
                            title=getattr(instruction, "title", None),
                            message="Edit could not be staged. Please try again.",
                            rejected_reason="stage_failed",
                        ).model_dump(),
                        "observation": {
                            "summary": "Edit rejected (stage_failed): could not stage the edit",
                            "artifacts": [],
                        },
                    }
                )
                return

            # Build summary of changes
            changes = []
            if data.text is not None:
                changes.append("text")
            if data.title is not None:
                changes.append("title")
            if data.category is not None:
                changes.append(f"category={data.category}")
            if data.confidence is not None:
                changes.append(f"confidence={data.confidence}")
            if data.load_mode is not None:
                changes.append(f"load_mode={data.load_mode}")
            status_changed = data.status is not None and new_status != base_status
            if status_changed:
                changes.append(f"status={data.status}")
            if data.table_names is not None:
                changes.append(f"tables={matched_table_names}")
            if shared is not None:
                changes.append(f"shared_with={shared['added']}")

            changes_str = ", ".join(changes) if changes else "no changes"
            version_str = f" (v{version_number})" if version_number else ""

            logger.info(f"Edited instruction {instruction.id}{version_str}: {changes_str}")

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=True,
                        instruction_id=str(instruction.id),
                        title=getattr(instruction, "title", None),
                        version_number=version_number,
                        build_id=str(training_build_id) if training_build_id else None,
                        message=f"Instruction updated successfully{version_str}",
                        previous_text=previous_text,
                        new_text=(new_text if data.text is not None else None),
                        previous_status=(DB_TO_STATUS.get(base_status) if status_changed else None),
                        new_status=(data.status if status_changed else None),
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edited instruction{version_str}: {changes_str}",
                        # The resulting instruction text, so the NEXT anchored
                        # edit has something real to anchor on. Without it the
                        # model is blind after its first edit: the instructions
                        # context section renders the LIVE row, which is not
                        # mutated until promotion, while a second edit in the
                        # same build applies to the PENDING version (see
                        # _load_pending_version). Its anchor then misses,
                        # costing an anchor_not_found round trip — and a model
                        # that cannot anchor reliably reaches for a rewrite.
                        # Mirrors the `current_text` the failure paths return.
                        **({"new_text": new_text} if data.text is not None else {}),
                        "artifacts": [
                            {
                                "type": "instruction_edit",
                                "id": str(instruction.id),
                                "version_number": version_number,
                                "changes": changes,
                                "tables": matched_table_names,
                            }
                        ],
                    },
                }
            )

        except Exception as e:
            logger.exception(f"Failed to edit instruction: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Failed to edit instruction: {str(e)}",
                    "code": "EDIT_FAILED"
                }
            )
