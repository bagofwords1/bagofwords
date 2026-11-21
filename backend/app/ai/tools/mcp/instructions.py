"""MCP Tools: Instructions — list, read, create, edit and delete.

External LLM clients (Claude, Cursor, ...) get the same rules as the web app:
authority to change an instruction comes from ``manage_instructions`` on every
agent it's attached to (org-level for global rules) — see
``app/services/instruction_authority.py``, shared with the REST routes.
Org admins and agent managers publish directly on what they manage; a global
rule written by an agent-level manager lands as a suggestion for admin review.

Instructions are organizational knowledge that guide AI behavior when generating code
and analyzing data.
"""

import json
import re
from typing import Dict, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.ai.tools.implementations._instruction_window import read_window
from app.ai.tools.mcp.instruction_scope import instruction_builder, report_data_sources
from app.ai.tools.mcp.instruction_placement import PlacementRenderer, load_placements
from app.models.user import User
from app.models.organization import Organization
from app.core.permission_resolver import resolve_permissions
from app.services.instruction_service import InstructionService
from app.errors.redact import describe_for_log
from fastapi import HTTPException

from app.services.instruction_authority import (
    require_delete_authority,
    require_instruction_authority,
    require_scope_authority,
)
from app.schemas.instruction_schema import InstructionCreate, InstructionUpdate
from app.schemas.mcp import (
    MCPListInstructionsInput,
    MCPListInstructionsOutput,
    MCPInstructionItem,
    MCPCreateInstructionInput,
    MCPCreateInstructionOutput,
    MCPDeleteInstructionInput,
    MCPDeleteInstructionOutput,
    MCPReadInstructionInput,
    MCPReadInstructionOutput,
    MCPInstructionDetail,
    MCPInstructionAgentRef,
    MCPEditInstructionInput,
    MCPEditInstructionOutput,
)

import logging

logger = logging.getLogger(__name__)


async def get_user_permissions(
    db: AsyncSession,
    user: User,
    organization: Organization
) -> set:
    """Get user's org-level permissions via the RBAC resolver."""
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    return set(resolved.org_permissions)


def is_admin(permissions: set) -> bool:
    """MVP: instruction admin = org-level manage_instructions or full_admin_access."""
    return 'manage_instructions' in permissions or 'full_admin_access' in permissions


# One answer for "missing", "another org's" and "not yours": telling them apart
# would let a caller probe which ids exist.
NOT_FOUND = "Instruction not found, or you don't have permission to change it."

# The edit contract is the in-app agent's (app/ai/tools/implementations/
# edit_instruction.py): same anchor matching, same status words, same limits.
# What differs is the outcome — MCP edits publish like the web app's save,
# where the in-app agent stages a suggestion for review.
from app.ai.tools.implementations.edit_instruction import (  # noqa: E402
    DB_TO_STATUS,
    MIN_RESULT_TEXT_LENGTH,
    STATUS_TO_DB,
    VALID_CATEGORIES,
    VALID_LOAD_MODES,
    _apply_anchor_edit as apply_anchor_edit,
)


def _refusal(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return f"Permission denied: {detail}" if exc.status_code == 403 else detail
    return str(exc)[:500]


async def _can(check) -> bool:
    try:
        await check
        return True
    except HTTPException:
        return False


# list_instructions shows each instruction's opening chars only (it can return
# up to 100 of them); every item says when its text is shortened, and
# read_instruction returns the rest.
LIST_TEXT_PREVIEW_CHARS = 500


def _pending(schema) -> bool:
    return getattr(schema, "current_build_status", None) == "pending_approval"


async def _waiting_for_review(db, instruction_id) -> bool:
    """Whether the instruction's newest version is still outside the live
    (main) build — i.e. the change just written waits for review.

    Read from the builds themselves: the detail schema's pending badge skips
    a pending change whose text the live one already "covers", which is every
    deletion-only edit, and would report a curator's unreviewed cut as live.
    """
    from sqlalchemy import select
    from app.models.build_content import BuildContent
    from app.models.instruction import Instruction
    from app.models.instruction_build import InstructionBuild

    current = await db.scalar(select(Instruction.current_version_id).where(Instruction.id == str(instruction_id)))
    if current is None:
        return False
    live = await db.scalar(
        select(BuildContent.instruction_version_id)
        .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
        .where(BuildContent.instruction_id == str(instruction_id), InstructionBuild.is_main.is_(True),
               InstructionBuild.deleted_at.is_(None), BuildContent.deleted_at.is_(None))
        .limit(1)
    )
    if live is None:
        # Not in the live build: waiting, unless the org has no live build at
        # all (legacy data, read straight from the rows).
        org_id = select(Instruction.organization_id).where(Instruction.id == str(instruction_id)).scalar_subquery()
        has_main = await db.scalar(select(InstructionBuild.id).where(
            InstructionBuild.organization_id == org_id, InstructionBuild.is_main.is_(True),
            InstructionBuild.deleted_at.is_(None)).limit(1))
        return has_main is not None
    return str(live) != str(current)


async def _open_suggestion(db, organization, user, instruction_id) -> bool:
    """Whether a change to the instruction waits for review — the same answer
    as /instructions/pending-changes (per-hunk, authoritative)."""
    try:
        ids = await InstructionService().get_pending_change_instruction_ids(
            db, organization, user, candidate_ids=[str(instruction_id)],
        )
    except Exception:
        logger.debug("pending check failed for %s", instruction_id, exc_info=True)
        return await _waiting_for_review(db, instruction_id)
    return str(instruction_id) in {str(i) for i in ids}


def _detail(schema, *, can_edit: bool, window: Optional[Dict[str, Any]] = None,
            placement: Optional[Dict[str, Any]] = None, pending: Optional[bool] = None) -> MCPInstructionDetail:
    """The instruction as MCP returns it. ``window`` (from read_window) replaces
    the text with the part that was read and adds where it sits in the whole.
    ``placement`` (from PlacementRenderer) says where it applies, as the caller
    may see it."""
    placement = placement or {}
    return MCPInstructionDetail(
        id=str(schema.id),
        title=getattr(schema, "title", None),
        text=window["text"] if window is not None else (getattr(schema, "text", "") or ""),
        category=getattr(schema, "category", None),
        status=getattr(schema, "status", None),
        load_mode=getattr(schema, "load_mode", None),
        kind=getattr(schema, "kind", None),
        state=DB_TO_STATUS.get(getattr(schema, "status", None)),
        scope=placement.get("scope"),
        folders=placement.get("folders") or [],
        agents=[MCPInstructionAgentRef(**a) for a in placement.get("agents") or []],
        hidden_target_count=placement.get("hidden_target_count") or 0,
        applies_nowhere=placement.get("applies_nowhere"),
        pending_review=_pending(schema) if pending is None else pending,
        can_edit=can_edit,
        **(window["fields"] if window is not None else {}),
    )


async def _placement(db, organization, user, inst_id, renderer: Optional[PlacementRenderer] = None):
    renderer = renderer or PlacementRenderer(db, organization, user)
    found = (await load_placements(db, [inst_id])).get(str(inst_id))
    return await renderer.render(found) if found else None


def _in_effect(db_status, pending: bool, *, created: bool = False) -> bool:
    """Whether the rule reaches any report right now: it must be active, and a
    rule that was just created must not be waiting for approval (a pending
    edit leaves the live version in force)."""
    return DB_TO_STATUS.get(db_status) == "active" and not (created and pending)


# list_instructions fetches up to this many rows when it filters by placement,
# then pages the matches itself.
PLACEMENT_FILTER_FETCH = 1000


class ListInstructionsMCPTool(MCPTool):
    """List instructions from the main (live) build.
    
    Returns instructions visible to the user based on their permissions.
    Supports filtering by status, category, and text search.
    """
    
    name = "list_instructions"
    read_only_hint = True
    description = (
        "List instructions from the current live build. "
        "Instructions are organizational knowledge that guide AI behavior. "
        "Returns instructions visible to the user based on their permissions. "
        "Supports filtering by status, category, text search, folder_id, data_source_id and "
        "scope (global/targeted). Each item says where it applies: scope, folders and agents. "
        f"Each item's text is cut to its first {LIST_TEXT_PREVIEW_CHARS} chars when longer "
        "(text_truncated: true, text_length = full length) — call read_instruction for the "
        "full text before quoting, following or editing an instruction."
    )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPListInstructionsInput.model_json_schema()
    
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute list_instructions - returns instructions from main build."""
        
        input_data = MCPListInstructionsInput(**args)
        # edit_instruction speaks active/inactive; accept the same words here.
        if input_data.status in STATUS_TO_DB:
            input_data.status = STATUS_TO_DB[input_data.status]
        permissions = await get_user_permissions(db, user, organization)

        # View is implicit: instruction_service filters by accessible data sources.
        instruction_service = InstructionService()
        
        filtering = bool(input_data.folder_id or input_data.data_source_id or input_data.scope)

        # Get instructions from main build (build_id=None uses main)
        result = await instruction_service.get_instructions(
            db=db,
            organization=organization,
            current_user=user,
            skip=0,
            limit=PLACEMENT_FILTER_FETCH if filtering else input_data.limit,
            status=input_data.status,
            categories=[input_data.category] if input_data.category else None,
            include_own=True,
            include_drafts='view_hidden_instructions' in permissions,
            search=input_data.search,
            build_id=None,  # Main build
        )
        
        items = list(result.get("items", []))
        placements = await load_placements(db, [str(i.id) for i in items])
        total = result.get("total", 0)
        if filtering:
            def keep(item) -> bool:
                p = placements.get(str(item.id))
                if p is None:
                    return False
                if input_data.scope and p.scope != input_data.scope:
                    return False
                if input_data.folder_id and input_data.folder_id not in p.folder_ids:
                    return False
                if input_data.data_source_id and input_data.data_source_id not in p.agent_ids:
                    return False
                return True
            items = [i for i in items if keep(i)]
            total = len(items)
            items = items[: input_data.limit]

        renderer = PlacementRenderer(db, organization, user)
        # Map to output schema
        instructions = []
        for item in items:
            text = item.text or ""
            p = placements.get(str(item.id))
            view = await renderer.render(p) if p else {}
            instructions.append(MCPInstructionItem(
                id=str(item.id),
                title=item.title,
                text=text[:LIST_TEXT_PREVIEW_CHARS],
                text_truncated=len(text) > LIST_TEXT_PREVIEW_CHARS,
                text_length=len(text),
                category=item.category or "general",
                status=item.status or "published",
                load_mode=item.load_mode or "always",
                source_type=item.source_type or "user",
                scope=view.get("scope"),
                folders=view.get("folders") or [],
                agents=[MCPInstructionAgentRef(**a) for a in view.get("agents") or []],
                hidden_target_count=view.get("hidden_target_count") or 0,
            ))
        
        logger.info(f"list_instructions: returned {len(instructions)} instructions for user {user.id}")
        
        return MCPListInstructionsOutput(
            instructions=instructions,
            total=total,
        ).model_dump()


# One instruction_ids read returns at most this many chars (JSON-encoded) of
# instruction details; the ids past it come back in not_read_ids.
BATCH_READ_BUDGET = 100_000


class ReadInstructionMCPTool(MCPTool):
    """Read instructions in full, with their agent scope and review state."""

    name = "read_instruction"
    read_only_hint = True
    description = (
        "Read an instruction in full: text, category, load mode, where it applies (scope: "
        "'global' or 'targeted', with its folders and agents), whether a change is pending admin review, and whether you can edit it. "
        "Use it for the entries get_context lists without text (pass report_id; instruction_ids "
        "reads several at once). list_instructions truncates text — read the full text here "
        "before editing. "
        "A very long instruction comes back as its opening lines plus an outline, with "
        "truncated: true and next_offset — read on with offset (and limit; plus col_offset when "
        "next_col_offset is set), or pull the lines you need with grep_pattern. Range and grep "
        "text is verbatim."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPReadInstructionInput.model_json_schema()

    @staticmethod
    def _fail(message: str) -> Dict[str, Any]:
        return MCPReadInstructionOutput(success=False, error_message=message).model_dump()

    async def execute(self, args, db, user, organization):
        data = MCPReadInstructionInput(**args)
        if bool(data.instruction_id) == bool(data.instruction_ids):
            return self._fail("Give instruction_id or instruction_ids (one of them).")
        windowed = any(v is not None for v in (data.offset, data.col_offset, data.limit, data.grep_pattern))
        if data.instruction_ids and windowed:
            return self._fail(
                "offset / col_offset / limit / grep_pattern work on one instruction: "
                "use instruction_id with them."
            )

        report, in_report = None, None
        if data.report_id:
            try:
                report = await self._load_report(db, data.report_id, user, organization)
            except HTTPException:
                return self._fail("Report not found")
            # The report's scope is decided by the same builder get_context lists
            # with (global rules, rules on the report's agents, rules on its
            # folders — live, in mode/channel), so the two always agree. An empty
            # agent list is not "global": a folder-only rule has none.
            builder = instruction_builder(
                db, organization, user, report, await report_data_sources(db, report, user, organization),
            )
            wanted = [data.instruction_id] if data.instruction_id else list(dict.fromkeys(data.instruction_ids))
            # The text the report applies is the approved (live) version; a
            # change still waiting for review is not part of it.
            in_report = {
                str(it.id): it.text for it in await builder.load_instructions_by_ids(wanted, load_mode_filter=None)
            }

        def _text(schema) -> str:
            if in_report is not None and in_report.get(str(schema.id)) is not None:
                return in_report[str(schema.id)]
            return getattr(schema, "text", "") or ""

        svc = InstructionService()
        renderer = PlacementRenderer(db, organization, user)

        async def _open(inst_id: str):
            schema = await svc.get_instruction(db, inst_id, organization, user)
            # Same visibility as GET /instructions/{id}: rows on agents the caller
            # can't see stay hidden, even for admins who aren't members.
            if schema is None or not await svc.user_can_view_instruction(db, schema, user, organization):
                return None
            if in_report is not None and str(schema.id) not in in_report:
                return None
            return schema

        if data.instruction_id:
            schema = await _open(data.instruction_id)
            if schema is None:
                return self._fail("Instruction not found" + (" in this report" if report else ""))
            try:
                window = read_window(
                    _text(schema),
                    getattr(schema, "title", None) or str(schema.id)[:8],
                    offset=data.offset,
                    col_offset=data.col_offset,
                    limit=data.limit,
                    grep_pattern=data.grep_pattern,
                    ignore_case=data.ignore_case,
                    before=data.before,
                    after=data.after,
                    max_matches=data.max_matches,
                )
            except re.error as e:
                return self._fail(f"Invalid grep_pattern: {e}")
            await self._record_read(db, organization, user, report, schema)
            can_edit = await _can(require_instruction_authority(db, user, organization, schema, allow_owner=True))
            placement = await _placement(db, organization, user, schema.id, renderer)
            pending = await _open_suggestion(db, organization, user, schema.id)
            message = window["summary"]
            if report is not None and pending and _text(schema) != (getattr(schema, "text", "") or ""):
                message += (" A change to this instruction is waiting for review: this is the approved "
                            "text the report applies. Read it without report_id to see the pending text "
                            "before editing.")
            return MCPReadInstructionOutput(
                success=True,
                instruction=_detail(schema, can_edit=can_edit, window=window, placement=placement, pending=pending),
                message=message,
            ).model_dump()

        details, not_read, not_found, used = [], [], [], 2
        for inst_id in dict.fromkeys(data.instruction_ids):
            if details and used >= BATCH_READ_BUDGET:
                not_read.append(inst_id)
                continue
            schema = await _open(inst_id)
            if schema is None:
                not_found.append(inst_id)
                continue
            window = read_window(_text(schema), getattr(schema, "title", None) or str(schema.id)[:8])
            can_edit = await _can(require_instruction_authority(db, user, organization, schema, allow_owner=True))
            placement = await _placement(db, organization, user, schema.id, renderer)
            detail = _detail(schema, can_edit=can_edit, window=window, placement=placement,
                             pending=await _open_suggestion(db, organization, user, schema.id))
            cost = len(json.dumps(detail.model_dump(), default=str)) + 2
            if details and used + cost > BATCH_READ_BUDGET:
                not_read.append(inst_id)
                continue
            used += cost
            details.append(detail)
            await self._record_read(db, organization, user, report, schema)

        parts = [f"Read {len(details)} instruction(s)."]
        if any(d.truncated for d in details):
            parts.append("Some are too long to return whole (truncated: true) — read on with instruction_id + offset.")
        if not_read:
            parts.append(f"{len(not_read)} not read to stay within the size budget — read not_read_ids in another call.")
        if not_found:
            parts.append(
                f"{len(not_found)} not found" + (" in this report" if report else "") + " (not_found_ids)."
            )
        return MCPReadInstructionOutput(
            success=bool(details),
            instructions=details,
            not_read_ids=not_read or None,
            not_found_ids=not_found or None,
            message=" ".join(parts),
            error_message=None if details else "No instruction could be read.",
        ).model_dump()

    @staticmethod
    async def _record_read(db, organization, user, report, schema) -> None:
        """Record an on-demand read for the report (best-effort), like the in-app
        tool: read instructions climb the usage ranking, and with smart
        instructions on, the report's data tools hand them to their coder."""
        if report is None:
            return
        try:
            from app.services.instruction_usage_service import InstructionUsageService
            from app.schemas.instruction_usage_schema import InstructionUsageEventCreate

            await InstructionUsageService().record_usage_event(
                db,
                InstructionUsageEventCreate(
                    org_id=str(organization.id),
                    report_id=str(report.id),
                    instruction_id=str(schema.id),
                    user_id=str(user.id),
                    load_mode=getattr(schema, "load_mode", None) or "intelligent",
                    load_reason="on_demand",
                    source_type=getattr(schema, "source_type", None),
                    category=getattr(schema, "category", None),
                    title=getattr(schema, "title", None),
                ),
            )
        except Exception:
            logger.debug("read_instruction: usage event failed", exc_info=True)


def _dedupe(values) -> list:
    return list(dict.fromkeys(str(v).strip() for v in (values or []) if v and str(v).strip()))


NO_TARGET = (
    "Say where the rule applies: folder_ids and/or data_source_ids (browse_agents lists the ones "
    "you can use), or scope: 'global' for a rule that applies in every report (needs org-level "
    "instruction admin)."
)


class CreateInstructionMCPTool(MCPTool):
    """Create an instruction — same rules and outcome as creating it in the web app."""

    name = "create_instruction"
    description = (
        "Create a new instruction that guides AI behavior when generating code and analyzing data. "
        "Say where it applies: folder_ids (folders, from browse_agents) and/or data_source_ids "
        "(agents) — one rule can be shared across several folders and agents. A folder rule applies "
        "in reports scoped to that folder, for every agent in it including later ones, not in reports "
        "pinned to single agents; the response's applies_in says where. You need "
        "instruction-management rights on each. For a rule that applies in every report, pass "
        "scope: 'global' instead (org-level instruction admin only). folder_id (one owning "
        "folder) still works."
    )

    @property
    def required_ds_permission(self):
        return "manage_instructions"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPCreateInstructionInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        input_data = MCPCreateInstructionInput(**args)
        ds_ids = _dedupe(input_data.data_source_ids)
        folder_ids = _dedupe(([input_data.folder_id] if input_data.folder_id else []) + (input_data.folder_ids or []))

        def fail(msg, code):
            return MCPCreateInstructionOutput(
                success=False, requires_approval=False, error_message=msg, rejected_reason=code,
            ).model_dump()

        if input_data.scope == "global" and (ds_ids or folder_ids):
            return fail("A global rule applies in every report: pass scope 'global' without folders or "
                        "agents, or drop scope to attach it to them.", "invalid_input")
        if input_data.scope != "global" and not ds_ids and not folder_ids:
            return fail(NO_TARGET, "no_target")

        # The web app's gates (POST /instructions): manage_instructions on every
        # listed folder and agent; a rule with no target needs it org-wide.
        try:
            if folder_ids:
                from app.services.folder_service import lock_tree
                from app.services.folder_knowledge import require_capability
                await lock_tree(db, organization, user)
                for fid in folder_ids:
                    await require_capability(db, organization, user, fid, "manage_instructions")
            if ds_ids or not folder_ids:
                await require_scope_authority(db, user, organization, ds_ids)
        except HTTPException as e:
            return fail(_refusal(e), "permission_denied")

        svc = InstructionService()
        instruction_data = InstructionCreate(
            text=input_data.text,
            title=input_data.title,
            category=input_data.category,
            load_mode=input_data.load_mode,
            data_source_ids=ds_ids,
            status="published",
        )
        # A failed commit expires the session's objects; read the id now.
        user_id = str(user.id)
        try:
            result = await svc.create_instruction(
                db=db, instruction_data=instruction_data, current_user=user,
                organization=organization, folder_id=input_data.folder_id, target_folder_ids=folder_ids,
            )
        except Exception as e:
            # A DB error's text carries the title and text as bound parameters.
            logger.error("create_instruction failed for user %s: %s", user_id, describe_for_log(e))
            return fail(_refusal(e), "permission_denied" if getattr(e, "status_code", None) == 403 else "create_failed")

        # Report what the build layer actually did.
        pending = await _waiting_for_review(db, result.id)
        logger.info("create_instruction: %s by user %s pending=%s", result.id, user_id, pending)
        # Where it applies as this caller may see it; the inputs stand in when
        # the placement can't be read back.
        placement = await _placement(db, organization, user, result.id) or {
            "scope": "targeted" if (folder_ids or ds_ids) else "global",
            "folders": [{"id": f} for f in folder_ids], "agents": [{"id": a} for a in ds_ids],
        }
        in_effect = _in_effect(getattr(result, "status", None), pending, created=True)
        return MCPCreateInstructionOutput(
            success=True,
            instruction_id=str(result.id), folder_id=input_data.folder_id,
            scope="targeted" if (folder_ids or ds_ids) else "global",
            folder_ids=folder_ids, data_source_ids=ds_ids,
            applies_in=applies_in(placement, in_effect=in_effect, pending=pending),
            in_effect=in_effect,
            build_status="pending_approval" if pending else "approved",
            requires_approval=pending,
        ).model_dump()


def applies_in(placement: Optional[Dict[str, Any]], *, in_effect: bool = True, pending: bool = False) -> str:
    """Where a rule takes effect, in the words a client should repeat.

    ``placement`` is the caller's rendered view (PlacementRenderer), so the
    counts cover only the folders and agents this caller may see; the rest are
    mentioned without a number. A rule that is inactive, or was just created
    and waits for approval, reaches no report yet, and the text says so first.
    """
    placement = placement or {}
    if placement.get("scope") == "global":
        where = "every report"
    else:
        folders, agents = len(placement.get("folders") or []), len(placement.get("agents") or [])
        parts = []
        if folders:
            parts.append(
                f"reports scoped to its {folders} folder(s) (create_report agent_folder_ids), for every "
                "agent in them, including agents added later — not in reports pinned to individual agents or "
                "title-only reports"
            )
        if agents:
            parts.append(f"any report that uses one of its {agents} agent(s)")
        if placement.get("hidden_target_count"):
            parts.append("reports that reach its other folders or agents, which you can't see")
        if not parts:
            return "No report: it has no folder or agent."
        where = "in " + "; and in ".join(parts)
    if in_effect:
        return where[0].upper() + where[1:] + "."
    why = "it waits for approval" if pending else "it is inactive"
    return f"No report yet: {why}. Once it is live: {where}."


LAST_TARGET = (
    "Edit rejected: this would detach the rule from its last folder/agent. A rule is never made "
    "global by detaching it. To keep it, leave at least one folder or agent (or attach a new one "
    "in the same call). To stop it applying anywhere, set status: 'inactive' or delete it with "
    "delete_instruction. To apply it in every report, pass scope: 'global' (org-level "
    "instruction admin only)."
)


def _resolve_targets(current: list, replace, add, remove) -> list:
    out = list(current) if replace is None else _dedupe(replace)
    out += [x for x in _dedupe(add) if x not in out]
    gone = set(_dedupe(remove))
    return [x for x in out if x not in gone]


class EditInstructionMCPTool(MCPTool):
    """Edit an existing instruction.

    Input contract = the in-app agent's edit_instruction (anchored text edits,
    active/inactive status, the same limits). Outcome and rights = the web
    app's save (PUT /instructions/{id}): published directly, by someone with
    authority over the instruction and over any new scope.
    """

    name = "edit_instruction"
    description = (
        "Edit an existing instruction; the change is published immediately. "
        "Text edits are anchored: pass `old_text` (an exact, unique snippet of the current text — "
        "read_instruction first) and `text` (its replacement). Only for an explicit rewrite, set "
        "replace_entire_text: true. Make several small anchored edits rather than one rewrite. "
        "status 'inactive' turns it off, 'active' back on. Where it applies: add_folder_ids / "
        "remove_folder_ids and add_data_source_ids / remove_data_source_ids attach or detach "
        "single targets and keep the rest (folder_ids / data_source_ids replace the whole list). "
        "Text-only edits keep every placement. Detaching the last target is refused — a rule "
        "only becomes global with scope: 'global'. You need instruction-management rights on "
        "every folder and agent it applies to (and on any you add); global instructions need "
        "org-level instruction admin."
    )

    @property
    def required_ds_permission(self):
        return "manage_instructions"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPEditInstructionInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        data = MCPEditInstructionInput(**args)

        def fail(msg, code, **extra):
            return MCPEditInstructionOutput(
                success=False, instruction_id=data.instruction_id,
                error_message=msg, rejected_reason=code, **extra,
            ).model_dump()

        # ── input checks (in-app tool's rules) ──
        if data.load_mode is not None and data.load_mode not in VALID_LOAD_MODES:
            return fail(
                f"Invalid load_mode '{data.load_mode}'. Use one of: {', '.join(sorted(VALID_LOAD_MODES))}. "
                "To take an instruction out of use, set status: 'inactive'.",
                "invalid_load_mode",
            )
        if data.category is not None and data.category not in VALID_CATEGORIES:
            return fail(
                f"Invalid category '{data.category}'. Use one of: {', '.join(sorted(VALID_CATEGORIES))}.",
                "invalid_category",
            )
        if (data.old_text is not None or data.replace_entire_text) and data.text is None:
            return fail("old_text / replace_entire_text need `text` (the replacement).", "invalid_input")
        target_args = (data.data_source_ids, data.add_data_source_ids, data.remove_data_source_ids,
                       data.folder_ids, data.add_folder_ids, data.remove_folder_ids)
        if data.scope == "global" and any(v for v in target_args):
            return fail("scope 'global' detaches the rule from every folder and agent: don't pass "
                        "folders or agents with it.", "invalid_input")
        if not any(v is not None for v in (
            data.text, data.title, data.category, data.load_mode, data.status, data.scope, *target_args,
        )):
            return fail("Nothing to change: pass at least one field.", "no_change")

        # ── placement: where the rule applies after this edit ──
        svc = InstructionService()
        current = (await load_placements(db, [data.instruction_id])).get(str(data.instruction_id))
        if current is None:
            return fail(NOT_FOUND, "not_found")
        to_global = data.scope == "global"
        if to_global:
            new_folders, new_agents = [], []
        else:
            new_folders = _resolve_targets(current.folder_ids, data.folder_ids, data.add_folder_ids, data.remove_folder_ids)
            new_agents = _resolve_targets(current.agent_ids, data.data_source_ids, data.add_data_source_ids,
                                          data.remove_data_source_ids)
        folders_changed = set(new_folders) != set(current.folder_ids)
        agents_changed = set(new_agents) != set(current.agent_ids)
        placement_changed = folders_changed or agents_changed or (to_global and current.scope != "global")

        # ── rights (web app's rules) ──
        if placement_changed:
            from app.services.folder_service import lock_tree
            await lock_tree(db, organization, user)
        existing = await svc.get_instruction(db, data.instruction_id, organization, user)
        if existing is None:
            return fail(NOT_FOUND, "not_found")
        try:
            # Changing where a rule applies is governance: the author carve-out
            # for a scope-less rule covers content only (as PUT /instructions).
            await require_instruction_authority(db, user, organization, existing, allow_owner=not placement_changed)
        except HTTPException:
            if await svc.user_can_view_instruction(db, existing, user, organization):
                return fail(
                    "Permission denied: you can read this instruction but not change it. Changing "
                    "it needs instruction-management rights on every folder and agent it applies to "
                    "(including ones you may not see).",
                    "permission_denied",
                )
            return fail(NOT_FOUND, "not_found")
        if placement_changed and not to_global and not new_folders and not new_agents:
            # Never silently global, never left active with nowhere to apply.
            return fail(LAST_TARGET, "would_remove_last_target")
        try:
            if placement_changed:
                from app.services.folder_knowledge import require_capability
                for fid in new_folders:
                    await require_capability(db, organization, user, fid, "manage_instructions")
                if new_agents or not new_folders:
                    # [] (scope 'global') needs org-level instruction admin.
                    await require_scope_authority(db, user, organization, new_agents)
        except HTTPException as e:
            return fail(_refusal(e), "permission_denied")

        # ── resulting text (anchor semantics, as in-app) ──
        current_text = existing.text or ""
        new_text = None
        if data.text is not None:
            if data.replace_entire_text:
                new_text = data.text
            elif data.old_text is None or data.old_text == "":
                return fail(
                    "Edit rejected: `text` needs an anchor. Pass `old_text` with an exact snippet of "
                    "the current text to replace it (\"\" is not an anchor; to add, anchor the "
                    "sentence it belongs after and repeat it before your addition). Only for an "
                    "explicit rewrite, re-send with replace_entire_text: true.",
                    "anchor_required", current_text=current_text,
                )
            else:
                applied, code, detail = apply_anchor_edit(current_text, data.old_text, data.text)
                if code:
                    return fail(f"Edit rejected: {detail}", code, current_text=current_text)
                new_text = applied
            if len(new_text.strip()) < MIN_RESULT_TEXT_LENGTH:
                return fail(
                    f"Edit rejected: the resulting text would be shorter than {MIN_RESULT_TEXT_LENGTH} characters.",
                    "result_too_short", current_text=current_text,
                )

        # ── status (active/inactive ↔ published/draft, as in-app) ──
        base_status = existing.status
        new_db_status = None
        if data.status is not None:
            if base_status not in DB_TO_STATUS:
                return fail(
                    f"This instruction is {base_status}; only active and inactive instructions can "
                    "be switched on or off.", "status_change_not_allowed",
                )
            new_db_status = STATUS_TO_DB[data.status]
            only_status = not placement_changed and all(v is None for v in (
                data.text, data.title, data.category, data.load_mode,
            ))
            if new_db_status == base_status and only_status:
                return fail(f"The instruction is already {data.status}.", "no_change")

        # Only the fields being changed: the admin edit path applies every
        # field that is *set*, so an explicit None would blank a column.
        changes = {
            "text": new_text, "title": data.title, "category": data.category,
            "load_mode": data.load_mode, "status": new_db_status,
            # An explicit agent list is passed through even when unchanged (a
            # no-op re-scope succeeds, as a save in the web app does).
            "data_source_ids": new_agents if (agents_changed or to_global or data.data_source_ids is not None) else None,
        }
        update = InstructionUpdate(**{k: v for k, v in changes.items() if v is not None})
        # A failed commit expires the session's objects; read the id now.
        user_id = str(user.id)
        try:
            updated = await svc.update_instruction(
                db, data.instruction_id, update, organization, user,
                target_folder_ids=new_folders if (folders_changed or to_global) else None,
            )
            if updated is not None and current.owner_folder_id and current.owner_folder_id not in new_folders:
                # Detached from its owning folder: it no longer belongs to that
                # folder, so deleting that folder later must not take it along.
                from sqlalchemy import update as sql_update
                from app.models.instruction import Instruction
                await db.execute(sql_update(Instruction).where(Instruction.id == data.instruction_id)
                                 .values(owner_folder_id=None))
                await db.commit()
        except HTTPException as e:
            logger.info("edit_instruction %s by %s refused: %s", data.instruction_id, user_id, e.detail)
            return fail(_refusal(e), "permission_denied" if e.status_code == 403 else "invalid_input")
        except Exception as e:
            logger.error("edit_instruction %s by %s failed: %s", data.instruction_id, user_id, describe_for_log(e))
            return fail(_refusal(e), "edit_failed")
        if updated is None:
            return fail(NOT_FOUND, "not_found")

        fresh = await svc.get_instruction(db, data.instruction_id, organization, user) or updated
        pending = await _waiting_for_review(db, data.instruction_id)
        placement = await _placement(db, organization, user, data.instruction_id)
        in_effect = _in_effect(getattr(fresh, "status", None), pending)
        status_changed = new_db_status is not None and new_db_status != base_status
        logger.info("edit_instruction: %s by user %s pending=%s", data.instruction_id, user_id, pending)
        return MCPEditInstructionOutput(
            success=True,
            instruction_id=data.instruction_id,
            build_status="pending_approval" if pending else "approved",
            requires_approval=pending,
            previous_text=current_text if new_text is not None else None,
            new_text=fresh.text if new_text is not None else None,
            previous_status=DB_TO_STATUS.get(base_status) if status_changed else None,
            new_status=data.status if status_changed else None,
            instruction=_detail(fresh, can_edit=True, pending=pending, placement=placement),
            applies_in=applies_in(placement, in_effect=in_effect) if placement else None,
            in_effect=in_effect if placement else None,
        ).model_dump()


class DeleteInstructionMCPTool(MCPTool):
    """Delete an instruction — same rules as the web app's delete."""

    name = "delete_instruction"
    read_only_hint = False
    destructive_hint = True
    description = (
        "Delete an instruction (soft delete); it's removed from the live build. "
        "You need instruction-management rights on every agent it applies to; global "
        "instructions need org-level instruction admin. You can always delete your own "
        "unpublished suggestion or your own global instruction."
    )

    @property
    def required_ds_permission(self):
        return "manage_instructions"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPDeleteInstructionInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        input_data = MCPDeleteInstructionInput(**args)
        svc = InstructionService()
        existing = await svc.get_instruction_access_view(db, input_data.instruction_id, organization)
        if existing is None:
            return MCPDeleteInstructionOutput(success=False, error_message=NOT_FOUND).model_dump()
        # The service trusts its caller on authority (the REST decorator +
        # route checks); this tool is that caller.
        try:
            await require_delete_authority(db, user, organization, existing)
        except HTTPException:
            return MCPDeleteInstructionOutput(success=False, error_message=NOT_FOUND).model_dump()
        try:
            success = await svc.delete_instruction(
                db=db, instruction_id=input_data.instruction_id,
                organization=organization, current_user=user,
            )
        except Exception as e:
            logger.error("delete_instruction failed for %s: %s", input_data.instruction_id, describe_for_log(e))
            return MCPDeleteInstructionOutput(success=False, error_message=_refusal(e)).model_dump()
        logger.info("delete_instruction: %s by user %s", input_data.instruction_id, user.id)
        return MCPDeleteInstructionOutput(success=bool(success)).model_dump()
