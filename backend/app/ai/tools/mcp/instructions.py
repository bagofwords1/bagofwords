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

from typing import Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.user import User
from app.models.organization import Organization
from app.core.permission_resolver import resolve_permissions
from app.services.instruction_service import InstructionService
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

VALID_STATUSES = {"published", "draft", "archived"}
# 'disabled' is not offered: it reads as "off" but used to widen loading.
# Turning a rule off is status='archived'.
VALID_LOAD_MODES = {"always", "intelligent"}


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


def _pending(schema) -> bool:
    return getattr(schema, "current_build_status", None) == "pending_approval"


def _detail(schema, *, can_edit: bool) -> MCPInstructionDetail:
    return MCPInstructionDetail(
        id=str(schema.id),
        title=getattr(schema, "title", None),
        text=getattr(schema, "text", "") or "",
        category=getattr(schema, "category", None),
        status=getattr(schema, "status", None),
        load_mode=getattr(schema, "load_mode", None),
        kind=getattr(schema, "kind", None),
        agents=[
            MCPInstructionAgentRef(id=str(ds.id), name=getattr(ds, "name", None))
            for ds in (getattr(schema, "data_sources", None) or [])
        ],
        pending_review=_pending(schema),
        can_edit=can_edit,
    )


class ListInstructionsMCPTool(MCPTool):
    """List instructions from the main (live) build.
    
    Returns instructions visible to the user based on their permissions.
    Supports filtering by status, category, and text search.
    """
    
    name = "list_instructions"
    description = (
        "List instructions from the current live build. "
        "Instructions are organizational knowledge that guide AI behavior. "
        "Returns instructions visible to the user based on their permissions. "
        "Supports filtering by status, category, and text search."
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
        instructions = [
            MCPInstructionItem(
                id=str(item.id),
                title=item.title,
                text=item.text[:500] if item.text else "",  # Truncate for response size
                category=item.category or "general",
                status=item.status or "published",
                load_mode=item.load_mode or "always",
                source_type=item.source_type or "user",
            )
            for item in result.get("items", [])
        ]
        
        logger.info(f"list_instructions: returned {len(instructions)} instructions for user {user.id}")
        
        return MCPListInstructionsOutput(
            instructions=instructions,
            total=result.get("total", 0),
        ).model_dump()


class ReadInstructionMCPTool(MCPTool):
    """Read one instruction in full, with its agent scope and review state."""

    name = "read_instruction"
    description = (
        "Read one instruction in full: text, category, load mode, the agents it applies to "
        "(empty = global), whether a change is pending admin review, and whether you can edit it. "
        "list_instructions truncates text — read the full text here before editing."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPReadInstructionInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        data = MCPReadInstructionInput(**args)
        svc = InstructionService()
        schema = await svc.get_instruction(db, data.instruction_id, organization, user)
        # Same visibility as GET /instructions/{id}: rows on agents the caller
        # can't see stay hidden, even for admins who aren't members.
        if schema is None or not await svc.user_can_view_instruction(db, schema, user, organization):
            return MCPReadInstructionOutput(success=False, error_message="Instruction not found").model_dump()
        can_edit = await _can(require_instruction_authority(db, user, organization, schema, allow_owner=True))
        return MCPReadInstructionOutput(success=True, instruction=_detail(schema, can_edit=can_edit)).model_dump()


class CreateInstructionMCPTool(MCPTool):
    """Create an instruction, scoped to agents the caller manages instructions on."""

    name = "create_instruction"
    description = (
        "Create a new instruction that guides AI behavior when generating code and analyzing data. "
        "Pass data_source_ids to scope it to agents you manage instructions on; it goes live at once. "
        "Without data_source_ids it is global (every agent): org instruction admins publish it, "
        "anyone else's global rule is submitted for admin approval."
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

        # Agent-scoped rules need manage_instructions on every listed agent —
        # the POST /instructions gate. Global rules stay open to anyone who
        # manages instructions somewhere (the call gate), but only org admins'
        # go live; the build layer routes the rest to review.
        if ds_ids:
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
            status="published",  # the build decides whether it goes live
        )
        try:
            result = await svc.create_instruction(
                db=db, instruction_data=instruction_data, current_user=user,
                organization=organization, force_global=True,
            )
        except Exception as e:
            logger.exception(f"create_instruction failed for user {user.id}: {e}")
            return MCPCreateInstructionOutput(
                success=False, requires_approval=False, error_message=_refusal(e),
            ).model_dump()

        # Report what actually happened, not a guess from org-level perms:
        # agent managers publish on their own agents.
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
    """Edit an existing instruction — same rules as the web app's edit."""

    name = "edit_instruction"
    description = (
        "Edit an existing instruction. Omitted fields stay unchanged; `text` replaces the whole text, "
        "so read_instruction first and send the complete new text. data_source_ids replaces the agent "
        "scope ([] = global). You need instruction-management rights on every agent the instruction "
        "applies to (and on the new scope); global instructions need org-level instruction admin. "
        "To turn an instruction off, set status='archived'."
    )

    @property
    def required_ds_permission(self):
        return "manage_instructions"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPEditInstructionInput.model_json_schema()

    async def execute(self, args, db, user, organization):
        data = MCPEditInstructionInput(**args)
        fail = lambda msg: MCPEditInstructionOutput(  # noqa: E731
            success=False, instruction_id=data.instruction_id, error_message=msg,
        ).model_dump()

        if data.status is not None and data.status not in VALID_STATUSES:
            return fail(f"Invalid status '{data.status}'. Use one of: {', '.join(sorted(VALID_STATUSES))}.")
        if data.load_mode is not None and data.load_mode not in VALID_LOAD_MODES:
            return fail(
                f"Invalid load_mode '{data.load_mode}'. Use one of: {', '.join(sorted(VALID_LOAD_MODES))}. "
                "To turn an instruction off, set status='archived'."
            )
        if data.text is not None and not data.text.strip():
            return fail("text cannot be empty.")
        changes = data.model_dump(exclude_none=True, exclude={"instruction_id"})
        if not changes:
            return fail("Nothing to change: pass at least one field.")

        svc = InstructionService()
        existing = await svc.get_instruction(db, data.instruction_id, organization, user)
        if existing is None:
            return fail(NOT_FOUND)
        # Same order as PUT /instructions/{id}: authority over the row as it is,
        # then over the scope it's being moved to.
        try:
            await require_instruction_authority(db, user, organization, existing, allow_owner=True)
        except HTTPException:
            if await svc.user_can_view_instruction(db, existing, user, organization):
                return fail("Permission denied: you can read this instruction but not change it. "
                            "Changing it needs instruction-management rights on every agent it applies to.")
            return fail(NOT_FOUND)
        try:
            await require_scope_authority(db, user, organization, data.data_source_ids)
        except HTTPException as e:
            return fail(_refusal(e))

        update = InstructionUpdate(**changes)
        try:
            updated = await svc.update_instruction(db, data.instruction_id, update, organization, user)
        except HTTPException as e:
            logger.info("edit_instruction %s by %s refused: %s", data.instruction_id, user.id, e.detail)
            return fail(_refusal(e))
        except Exception as e:
            logger.exception("edit_instruction %s by %s failed: %s", data.instruction_id, user.id, e)
            return fail(_refusal(e))
        if updated is None:
            return fail(NOT_FOUND)

        fresh = await svc.get_instruction(db, data.instruction_id, organization, user) or updated
        pending = _pending(fresh)
        logger.info("edit_instruction: %s by user %s fields=%s pending=%s",
                    data.instruction_id, user.id, sorted(changes), pending)
        return MCPEditInstructionOutput(
            success=True,
            instruction_id=data.instruction_id,
            build_status="pending_approval" if pending else "approved",
            requires_approval=pending,
            instruction=_detail(fresh, can_edit=True),
        ).model_dump()


class DeleteInstructionMCPTool(MCPTool):
    """Delete an instruction — same rules as the web app's delete."""

    name = "delete_instruction"
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
            logger.exception(f"delete_instruction failed for {input_data.instruction_id}: {e}")
            return MCPDeleteInstructionOutput(success=False, error_message=_refusal(e)).model_dump()
        logger.info("delete_instruction: %s by user %s", input_data.instruction_id, user.id)
        return MCPDeleteInstructionOutput(success=bool(success)).model_dump()
