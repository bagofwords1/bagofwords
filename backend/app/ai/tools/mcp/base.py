"""Base class for MCP tools."""

import datetime
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, TypedDict

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping
from app.models.completion_block import CompletionBlock


class TrackingContext(TypedDict):
    """Context returned by _create_tracking_context for tracking MCP tool execution."""
    platform: ExternalPlatform
    head_completion: Completion
    system_completion: Completion
    agent_execution: AgentExecution


class MCPTool(ABC):
    """Base class for MCP tools.
    
    MCP tools are exposed via the /mcp API to external LLMs like Claude and Cursor.
    They receive authenticated user/organization context and can call internal services.
    """
    
    name: str
    description: str
    
    @property
    @abstractmethod
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema for tool input arguments."""
        pass
    
    @abstractmethod
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute the tool with authenticated context.
        
        Args:
            args: Tool arguments from the MCP client
            db: Database session
            user: Authenticated user (from API key)
            organization: User's organization (from API key)
            
        Returns:
            Tool result as a dictionary
        """
        pass
    
    def to_schema(self) -> Dict[str, Any]:
        """Convert tool to MCP schema format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
    
    # ==================== Tracking Helpers ====================
    
    async def _get_or_create_mcp_platform(
        self, 
        db: AsyncSession, 
        organization: Organization
    ) -> ExternalPlatform:
        """Get or create the MCP ExternalPlatform for an organization (lazy creation).
        
        Called on first MCP API call to create the platform record that enables
        report tracking and icon display.
        """
        from app.services.external_platform_service import ExternalPlatformService
        
        service = ExternalPlatformService()
        try:
            platform = await service.get_platform_by_type(db, str(organization.id), "mcp")
        except Exception:
            platform = None
        
        if not platform:
            platform = ExternalPlatform(
                organization_id=str(organization.id),
                platform_type="mcp",
                platform_config={"name": "MCP Integration", "auto_created": True},
                is_active=True,
            )
            db.add(platform)
            await db.flush()
        
        return platform
    
    async def _ensure_mcp_user_mapping(
        self, 
        db: AsyncSession, 
        user: User, 
        organization: Organization,
        platform: ExternalPlatform
    ) -> None:
        """Create ExternalUserMapping for MCP if not exists.
        
        This enables the MCP icon to appear in the Members page for users
        who have made MCP API calls.
        """
        # Check if mapping already exists
        stmt = select(ExternalUserMapping).where(
            and_(
                ExternalUserMapping.app_user_id == str(user.id),
                ExternalUserMapping.platform_id == str(platform.id),
                ExternalUserMapping.platform_type == "mcp",
            )
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        
        if not existing:
            mapping = ExternalUserMapping(
                organization_id=str(organization.id),
                app_user_id=str(user.id),
                platform_id=str(platform.id),
                platform_type="mcp",
                external_user_id=f"mcp_{user.id}",  # Synthetic ID for MCP
                external_email=user.email,
                external_name=user.name,
                is_verified=True,
            )
            db.add(mapping)
            await db.flush()
    
    async def _create_tracking_context(
        self,
        db: AsyncSession,
        user: User,
        organization: Organization,
        report: Report,
        tool_name: str,
        args: Dict[str, Any],
    ) -> TrackingContext:
        """Create full tracking context: Platform, Completions, AgentExecution.
        
        This creates the audit trail for MCP tool calls so they appear in the
        report's conversation history.
        
        Returns:
            TrackingContext with platform, completions, and agent_execution
        """
        from app.project_manager import ProjectManager
        from app.settings.config import settings
        
        # Get or create MCP platform
        platform = await self._get_or_create_mcp_platform(db, organization)
        
        # Ensure user mapping exists
        await self._ensure_mcp_user_mapping(db, user, organization, platform)
        
        # Create head completion (user message)
        prompt_content = args.get("prompt", "") or args.get("title", "") or f"{tool_name} call"
        head_completion = Completion(
            prompt={"content": f"{tool_name}: {prompt_content}"},
            completion={},
            status="success",
            model="mcp",
            role="user",
            message_type="user_prompt",
            report_id=str(report.id),
            user_id=str(user.id),
            external_platform="mcp",
        )
        db.add(head_completion)
        await db.flush()
        
        # Create system completion (AI response)
        system_completion = Completion(
            prompt={},
            completion={},
            status="in_progress",
            model="mcp",
            role="system",
            message_type="ai_completion",
            parent_id=str(head_completion.id),
            report_id=str(report.id),
            user_id=str(user.id),
            external_platform="mcp",
        )
        db.add(system_completion)
        await db.flush()
        
        # Create AgentExecution
        agent_execution = AgentExecution(
            completion_id=str(system_completion.id),
            organization_id=str(organization.id),
            user_id=str(user.id),
            report_id=str(report.id),
            status="in_progress",
            started_at=datetime.datetime.utcnow(),
            config_json={"source": "mcp", "tool": tool_name},
            bow_version=settings.PROJECT_VERSION,
        )
        db.add(agent_execution)
        await db.flush()
        
        return TrackingContext(
            platform=platform,
            head_completion=head_completion,
            system_completion=system_completion,
            agent_execution=agent_execution,
        )
    
    async def _finish_tracking(
        self,
        db: AsyncSession,
        tracking: TrackingContext,
        success: bool,
        summary: str,
        result_json: Optional[Dict[str, Any]] = None,
        created_step_id: Optional[str] = None,
        created_visualization_ids: Optional[list] = None,
    ) -> None:
        """Finish tracking: create ToolExecution, CompletionBlock, update completion status.
        
        Args:
            db: Database session
            tracking: TrackingContext from _create_tracking_context
            success: Whether the tool execution succeeded
            summary: Summary message for the completion
            result_json: Optional result data to store
            created_step_id: Optional step ID if one was created
            created_visualization_ids: Optional list of visualization IDs
        """
        from app.models.tool_execution import ToolExecution
        
        agent_execution = tracking["agent_execution"]
        system_completion = tracking["system_completion"]
        now = datetime.datetime.utcnow()
        
        # Create ToolExecution record
        tool_execution = ToolExecution(
            agent_execution_id=str(agent_execution.id),
            plan_decision_id=None,  # MCP doesn't use plan decisions
            tool_name=self.name,
            tool_action=self.name,
            arguments_json={},
            status="success" if success else "error",
            started_at=agent_execution.started_at,
            completed_at=now,
            result_summary=summary[:500] if summary else None,
            result_json=result_json,
            created_step_id=created_step_id,
            artifact_refs_json={"visualizations": created_visualization_ids} if created_visualization_ids else None,
        )
        db.add(tool_execution)
        await db.flush()  # Flush to get tool_execution.id
        
        # Create CompletionBlock linking system_completion to tool_execution
        # This is required for the UI to display the tool execution in the report
        completion_block = CompletionBlock(
            completion_id=str(system_completion.id),
            agent_execution_id=str(agent_execution.id),
            source_type="tool",
            tool_execution_id=str(tool_execution.id),
            block_index=1,
            title=self.name,
            status="success" if success else "error",
            icon="🔧",
            content=summary[:1000] if summary else None,
            started_at=agent_execution.started_at,
            completed_at=now,
        )
        db.add(completion_block)
        
        # Update agent execution
        agent_execution.status = "completed" if success else "error"
        agent_execution.completed_at = now
        
        # Update system completion
        system_completion.status = "success" if success else "error"
        system_completion.completion = {"content": summary}
        
        await db.commit()
