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
from app.ai.tools.mcp.instruction_scope import in_scope, report_data_sources
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


def _detail(schema, *, can_edit: bool, window: Optional[Dict[str, Any]] = None) -> MCPInstructionDetail:
    """The instruction as MCP returns it. ``window`` (from read_window) replaces
    the text with the part that was read and adds where it sits in the whole."""
    return MCPInstructionDetail(
        id=str(schema.id),
        title=getattr(schema, "title", None),
        text=window["text"] if window is not None else (getattr(schema, "text", "") or ""),
        category=getattr(schema, "category", None),
        status=getattr(schema, "status", None),
        load_mode=getattr(schema, "load_mode", None),
        kind=getattr(schema, "kind", None),
        state=DB_TO_STATUS.get(getattr(schema, "status", None)),
        agents=[
            MCPInstructionAgentRef(id=str(ds.id), name=getattr(ds, "name", None))
            for ds in (getattr(schema, "data_sources", None) or [])
        ],
        pending_review=_pending(schema),
        can_edit=can_edit,
        **(window["fields"] if window is not None else {}),
    )


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
        "Supports filtering by status, category, and text search. "
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
        permissions = await get_user_permissions(db, user, organization)

        # View is implicit: instruction_service filters by accessible data sources.
        instruction_service = InstructionService()
        
        # Get instructions from main build (build_id=None uses main)
        result = await instruction_service.get_instructions(
            db=db,
            organization=organization,
            current_user=user,
            skip=0,
            limit=input_data.limit,
            status=input_data.status,
            categories=[input_data.category] if input_data.category else None,
            include_own=True,
            include_drafts='view_hidden_instructions' in permissions,
            search=input_data.search,
            build_id=None,  # Main build
        )
        
        # Map to output schema
        instructions = []
        for item in result.get("items", []):
            text = item.text or ""
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
            ))
        
        logger.info(f"list_instructions: returned {len(instructions)} instructions for user {user.id}")
        
        return MCPListInstructionsOutput(
            instructions=instructions,
            total=result.get("total", 0),
        ).model_dump()


# One instruction_ids read returns at most this many chars (JSON-encoded) of
# instruction details; the ids past it come back in not_read_ids.
BATCH_READ_BUDGET = 100_000


class ReadInstructionMCPTool(MCPTool):
    """Read instructions in full, with their agent scope and review state."""

    name = "read_instruction"
    read_only_hint = True
    description = (
        "Read an instruction in full: text, category, load mode, the agents it applies to "
        "(empty = global), whether a change is pending admin review, and whether you can edit it. "
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

        report, scope_ids = None, None
        if data.report_id:
            try:
                report = await self._load_report(db, data.report_id, user, organization)
            except HTTPException:
                return self._fail("Report not found")
            scope_ids = [str(ds.id) for ds in await report_data_sources(db, report, user, organization)]

        svc = InstructionService()

        async def _open(inst_id: str):
            schema = await svc.get_instruction(db, inst_id, organization, user)
            # Same visibility as GET /instructions/{id}: rows on agents the caller
            # can't see stay hidden, even for admins who aren't members.
            if schema is None or not await svc.user_can_view_instruction(db, schema, user, organization):
                return None
            agents = [ds.id for ds in (getattr(schema, "data_sources", None) or [])]
            if scope_ids is not None and not in_scope(agents, scope_ids):
                return None
            return schema

        if data.instruction_id:
            schema = await _open(data.instruction_id)
            if schema is None:
                return self._fail("Instruction not found" + (" in this report" if report else ""))
            try:
                window = read_window(
                    getattr(schema, "text", "") or "",
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
            return MCPReadInstructionOutput(
                success=True,
                instruction=_detail(schema, can_edit=can_edit, window=window),
                message=window["summary"],
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
            window = read_window(
                getattr(schema, "text", "") or "", getattr(schema, "title", None) or str(schema.id)[:8],
            )
            can_edit = await _can(require_instruction_authority(db, user, organization, schema, allow_owner=True))
            detail = _detail(schema, can_edit=can_edit, window=window)
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


class CreateInstructionMCPTool(MCPTool):
    """Create an instruction — same rules and outcome as creating it in the web app."""

    name = "create_instruction"
    description = (
        "Create a new instruction that guides AI behavior when generating code and analyzing data. "
        "It is published immediately. Pass data_source_ids to scope it to agents you manage "
        "instructions on; without them it is global (every agent), which needs org-level "
        "instruction admin."
    )

    @property
    def required_ds_permission(self):
        return "manage_instructions"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPCreateInstructionInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        input_data = MCPCreateInstructionInput(**args)
        ds_ids = [str(i) for i in (input_data.data_source_ids or [])]

        # The web app's gates: POST /instructions needs manage_instructions on
        # every listed agent; POST /instructions/global (no agents) needs it
        # org-wide. An agent manager can't author an org-wide rule.
        try:
            await require_scope_authority(db, user, organization, ds_ids)
        except HTTPException as e:
            return MCPCreateInstructionOutput(
                success=False, requires_approval=False, error_message=_refusal(e),
            ).model_dump()

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
                organization=organization, force_global=True,
            )
        except Exception as e:
            # A DB error's text carries the title and text as bound parameters.
            logger.error("create_instruction failed for user %s: %s", user_id, describe_for_log(e))
            return MCPCreateInstructionOutput(
                success=False, requires_approval=False, error_message=_refusal(e),
            ).model_dump()

        # Report what the build layer actually did.
        fresh = await svc.get_instruction(db, str(result.id), organization, user)
        pending = _pending(fresh) if fresh is not None else True
        logger.info("create_instruction: %s by user %s pending=%s", result.id, user.id, pending)
        return MCPCreateInstructionOutput(
            success=True,
            instruction_id=str(result.id),
            build_status="pending_approval" if pending else "approved",
            requires_approval=pending,
        ).model_dump()


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
        "status 'inactive' turns it off, 'active' back on. data_source_ids replaces the agent "
        "scope ([] = global). You need instruction-management rights on every agent it applies "
        "to (and on the new scope); global instructions need org-level instruction admin."
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
        if not any(v is not None for v in (
            data.text, data.title, data.category, data.load_mode, data.status, data.data_source_ids,
        )):
            return fail("Nothing to change: pass at least one field.", "no_change")

        # ── rights (web app's rules) ──
        svc = InstructionService()
        existing = await svc.get_instruction(db, data.instruction_id, organization, user)
        if existing is None:
            return fail(NOT_FOUND, "not_found")
        try:
            await require_instruction_authority(db, user, organization, existing, allow_owner=True)
        except HTTPException:
            if await svc.user_can_view_instruction(db, existing, user, organization):
                return fail(
                    "Permission denied: you can read this instruction but not change it. Changing "
                    "it needs instruction-management rights on every agent it applies to.",
                    "permission_denied",
                )
            return fail(NOT_FOUND, "not_found")
        try:
            await require_scope_authority(db, user, organization, data.data_source_ids)
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
            only_status = all(v is None for v in (
                data.text, data.title, data.category, data.load_mode, data.data_source_ids,
            ))
            if new_db_status == base_status and only_status:
                return fail(f"The instruction is already {data.status}.", "no_change")

        # Only the fields being changed: the admin edit path applies every
        # field that is *set*, so an explicit None would blank a column.
        changes = {
            "text": new_text, "title": data.title, "category": data.category,
            "load_mode": data.load_mode, "status": new_db_status,
            "data_source_ids": data.data_source_ids,
        }
        update = InstructionUpdate(**{k: v for k, v in changes.items() if v is not None})
        # A failed commit expires the session's objects; read the id now.
        user_id = str(user.id)
        try:
            updated = await svc.update_instruction(db, data.instruction_id, update, organization, user)
        except HTTPException as e:
            logger.info("edit_instruction %s by %s refused: %s", data.instruction_id, user_id, e.detail)
            return fail(_refusal(e), "permission_denied" if e.status_code == 403 else "invalid_input")
        except Exception as e:
            logger.error("edit_instruction %s by %s failed: %s", data.instruction_id, user_id, describe_for_log(e))
            return fail(_refusal(e), "edit_failed")
        if updated is None:
            return fail(NOT_FOUND, "not_found")

        fresh = await svc.get_instruction(db, data.instruction_id, organization, user) or updated
        pending = _pending(fresh)
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
            instruction=_detail(fresh, can_edit=True),
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
