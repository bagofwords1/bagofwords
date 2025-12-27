"""MCP Tools: Instructions - List, create, and delete instructions with permission-aware build integration.

These tools allow external LLM clients (Claude, Cursor, etc.) to manage instructions.
- Non-admins can suggest instructions (builds go to pending_approval for admin review)
- Admins get auto-approved builds that go live immediately

Instructions are organizational knowledge that guide AI behavior when generating code
and analyzing data.
"""

from typing import Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.user import User
from app.models.organization import Organization
from app.models.membership import Membership, ROLES_PERMISSIONS
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import InstructionCreate
from app.schemas.mcp import (
    MCPListInstructionsInput,
    MCPListInstructionsOutput,
    MCPInstructionItem,
    MCPCreateInstructionInput,
    MCPCreateInstructionOutput,
    MCPDeleteInstructionInput,
    MCPDeleteInstructionOutput,
)

import logging

logger = logging.getLogger(__name__)


async def get_user_permissions(
    db: AsyncSession, 
    user: User, 
    organization: Organization
) -> set:
    """Get user's permissions in the organization."""
    stmt = select(Membership).where(
        Membership.user_id == user.id,
        Membership.organization_id == organization.id
    )
    result = await db.execute(stmt)
    membership = result.scalar_one_or_none()
    return ROLES_PERMISSIONS.get(membership.role, set()) if membership else set()


def is_admin(permissions: set) -> bool:
    """Check if user has admin permissions for instructions."""
    return 'create_instructions' in permissions


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
        
        # Check view permission
        if 'view_instructions' not in permissions:
            logger.warning(f"User {user.id} lacks view_instructions permission")
            return MCPListInstructionsOutput(
                instructions=[],
                total=0,
            ).model_dump()
        
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


class CreateInstructionMCPTool(MCPTool):
    """Create an instruction with automatic build integration.
    
    Creates a new instruction that guides AI behavior. The instruction is
    automatically added to a build:
    - For admins: Build is auto-approved and goes live immediately
    - For non-admins: Build is submitted for admin approval (pending_approval status)
    """
    
    name = "create_instruction"
    description = (
        "Create a new instruction that guides AI behavior when generating code and analyzing data. "
        "Instructions are automatically versioned and added to a build. "
        "For admins: instruction goes live immediately. "
        "For non-admins: instruction is submitted for admin approval."
    )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPCreateInstructionInput.model_json_schema()
    
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute create_instruction with build integration."""
        
        input_data = MCPCreateInstructionInput(**args)
        permissions = await get_user_permissions(db, user, organization)
        
        # Check create permission - both admins and non-admins can create
        # Non-admins need 'suggest_instructions' or 'create_private_instructions'
        can_create = (
            'create_instructions' in permissions or 
            'create_private_instructions' in permissions or
            'suggest_instructions' in permissions
        )
        
        if not can_create:
            logger.warning(f"User {user.id} lacks permission to create instructions")
            return MCPCreateInstructionOutput(
                success=False,
                requires_approval=False,
                error_message="Permission denied: cannot create instructions."
            ).model_dump()
        
        instruction_service = InstructionService()
        
        # Build instruction create payload
        instruction_data = InstructionCreate(
            text=input_data.text,
            title=input_data.title,
            category=input_data.category,
            load_mode=input_data.load_mode,
            data_source_ids=input_data.data_source_ids or [],
            status="published",  # Always published - build handles approval workflow
        )
        
        try:
            # InstructionService.create_instruction handles:
            # 1. Create instruction record
            # 2. Create version snapshot
            # 3. Add version to build  
            # 4. Auto-finalize based on permissions:
            #    - Admin: approve + promote to main (live immediately)
            #    - Non-admin: submit for approval (pending_approval)
            result = await instruction_service.create_instruction(
                db=db,
                instruction_data=instruction_data,
                current_user=user,
                organization=organization,
                force_global=True,  # All instructions go through builds
            )
            
            # Determine approval status based on user role
            user_is_admin = is_admin(permissions)
            
            logger.info(
                f"create_instruction: created {result.id} by user {user.id}, "
                f"admin={user_is_admin}, requires_approval={not user_is_admin}"
            )
            
            return MCPCreateInstructionOutput(
                success=True,
                instruction_id=str(result.id),
                build_status="approved" if user_is_admin else "pending_approval",
                requires_approval=not user_is_admin,
            ).model_dump()
            
        except Exception as e:
            logger.exception(f"create_instruction failed for user {user.id}: {e}")
            return MCPCreateInstructionOutput(
                success=False,
                requires_approval=False,
                error_message=str(e)[:500],
            ).model_dump()


class DeleteInstructionMCPTool(MCPTool):
    """Delete an instruction with automatic build update.
    
    Soft-deletes an instruction and removes it from the current build.
    - Admins can delete any instruction
    - Non-admins can only delete their own instructions
    """
    
    name = "delete_instruction"
    description = (
        "Delete an instruction (soft delete). "
        "The instruction is removed from the current build. "
        "Admins can delete any instruction. Non-admins can only delete their own."
    )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPDeleteInstructionInput.model_json_schema()
    
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute delete_instruction with build update."""
        
        input_data = MCPDeleteInstructionInput(**args)
        permissions = await get_user_permissions(db, user, organization)
        
        # Check delete permission
        can_delete = (
            'delete_instructions' in permissions or
            'delete_private_instructions' in permissions
        )
        
        if not can_delete:
            logger.warning(f"User {user.id} lacks permission to delete instructions")
            return MCPDeleteInstructionOutput(
                success=False,
                error_message="Permission denied: cannot delete instructions."
            ).model_dump()
        
        instruction_service = InstructionService()
        
        try:
            # InstructionService.delete_instruction handles:
            # 1. Soft delete the instruction (set deleted_at)
            # 2. Remove from build
            # 3. Auto-finalize build
            # For non-admins, the service verifies ownership
            success = await instruction_service.delete_instruction(
                db=db,
                instruction_id=input_data.instruction_id,
                organization=organization,
                current_user=user,
            )
            
            logger.info(f"delete_instruction: deleted {input_data.instruction_id} by user {user.id}")
            
            return MCPDeleteInstructionOutput(
                success=success,
            ).model_dump()
            
        except Exception as e:
            logger.exception(f"delete_instruction failed for {input_data.instruction_id}: {e}")
            return MCPDeleteInstructionOutput(
                success=False,
                error_message=str(e)[:500],
            ).model_dump()


