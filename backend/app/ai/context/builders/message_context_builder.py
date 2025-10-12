"""
Message Context Builder - Ports proven logic from agent._build_messages_context()
"""
import json
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.completion import Completion
from app.models.widget import Widget
from app.models.step import Step
from app.models.organization import Organization
from app.ai.context.sections.messages_section import MessagesSection, MessageItem


class MessageContextBuilder:
    """
    Builds conversation message context for agent execution.
    
    Ports the proven logic from agent._build_messages_context() with
    completion history, widget associations, and step information.
    """
    
    def __init__(self, db: AsyncSession, organization, report, user=None):
        self.db = db
        self.report = report
        self.organization = organization
        self.organization_settings = organization.settings if organization else None
    
    async def build_context(
        self,
        max_messages: int = 20,
        role_filter: Optional[List[str]] = None
    ) -> str:
        """
        Build clean conversation context showing user prompts and system responses.
        
        Format:
        - User messages: show prompt content
        - System messages: show reasoning + assistant messages from completion blocks
        
        Args:
            max_messages: Maximum number of message pairs to include
            role_filter: Filter by specific roles (e.g., ['user', 'system'])
        
        Returns:
            Formatted conversation context string
        """
        from app.models.completion_block import CompletionBlock
        
        conversation = []
                   # Check organization settings for data visibility
        allow_llm_see_data = True
        if self.organization_settings:
            try:
                # Get the config dictionary from the organization settings
                settings_dict = self.organization_settings.config
                allow_llm_see_data = settings_dict.get("allow_llm_see_data", {}).get("value", True)
            except:
                allow_llm_see_data = False  # Default to True if settings unavailable
                    
        # Get all completions for this report ordered by creation time
        report_completions = await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.asc())
        )
        report_completions = report_completions.scalars().all()
        
        # Skip the last completion if it's from a user (current incomplete conversation)
        completions_to_process = (
            report_completions[:-1] 
            if report_completions and report_completions[-1].role == 'user' 
            else report_completions
        )
        
        # Apply role filter if provided
        if role_filter:
            completions_to_process = [
                c for c in completions_to_process 
                if c.role in role_filter
            ]
        
        # Limit to max_messages (considering pairs)
        completions_to_process = completions_to_process[-max_messages:]
        
        for completion in completions_to_process:
            timestamp = completion.created_at.strftime("%H:%M")
            
            if completion.role == 'user':
                # User message: show prompt content
                content = completion.prompt.get('content', '') if completion.prompt else ''
                if content.strip():
                    conversation.append(f"User ({timestamp}): {content.strip()}")
                    
            elif completion.role == 'system':
                # System message: get reasoning + assistant from completion blocks + tool executions
                blocks_result = await self.db.execute(
                    select(CompletionBlock)
                    .filter(CompletionBlock.completion_id == completion.id)
                    .order_by(CompletionBlock.block_index.asc())
                )
                blocks = blocks_result.scalars().all()
                
                system_parts = []
                
                # Collect reasoning, assistant messages, and tool executions from blocks
                for block in blocks:
                    # Don't truncate reasoning and content - show full text
                    if block.reasoning and block.reasoning.strip():
                        system_parts.append(f"Thinking: {block.reasoning.strip()}")
                    
                    if block.content and block.content.strip():
                        system_parts.append(f"Response: {block.content.strip()}")
                    
                    # Add tool execution details if available
                    if block.tool_execution_id:
                        from app.models.tool_execution import ToolExecution
                        tool_result = await self.db.execute(
                            select(ToolExecution).filter(ToolExecution.id == block.tool_execution_id)
                        )
                        tool_execution = tool_result.scalars().first()
                        
                        if tool_execution:
                            tool_info = f"Tool: {tool_execution.tool_name}"
                            if tool_execution.tool_action:
                                tool_info += f" → {tool_execution.tool_action}"
                            tool_info += f" ({tool_execution.status})"
                            
                
                            # Add widget/step information and data based on tool execution result
                            if tool_execution.status == 'success':
                                # Digest for create_widget results
                                if tool_execution.tool_name == 'create_widget' and tool_execution.result_json:
                                    result_json = tool_execution.result_json or {}
                                    widget_data = result_json.get('widget_data', {}) or {}
                                    columns = widget_data.get('columns', []) or []
                                    rows = widget_data.get('rows', []) or []
                                    col_names = [c.get('field') or c.get('headerName') for c in columns if (c.get('field') or c.get('headerName'))]
                                    row_count = len(rows)
                                    sample_row = None
                                    if allow_llm_see_data:
                                        preview = result_json.get('data_preview', {}) or {}
                                        preview_rows = preview.get('rows') or []
                                        if preview_rows:
                                            sample_row = preview_rows[0]
                                        elif rows:
                                            sample_row = rows[0]
                                    digest_parts = [f"{row_count} rows × {len(col_names)} cols"]
                                    if col_names:
                                        head_cols = ", ".join(col_names[:3])
                                        digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 3 else ''}")
                                    if sample_row:
                                        try:
                                            digest_parts.append(f"top row: {json.dumps(sample_row)}")
                                        except Exception:
                                            pass
                                    tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'answer_question' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    answer_text = rj.get('answer') or ((rj.get('output') or {}).get('answer') if isinstance(rj.get('output'), dict) else None)
                                    if answer_text:
                                        tool_info += f" - AI answer: {answer_text}"
                                elif tool_execution.created_widget_id:
                                    # Get widget details for other tools
                                    widget_result = await self.db.execute(
                                        select(Widget).filter(Widget.id == tool_execution.created_widget_id)
                                    )
                                    widget = widget_result.scalars().first()
                                    if widget:
                                        tool_info += f" - Widget: '{widget.title}'"
                                    else:
                                        tool_info += f" - Widget #{tool_execution.created_widget_id}"
                                
                                elif tool_execution.created_step_id:
                                    # Get step details for other tools
                                    step_result = await self.db.execute(
                                        select(Step).filter(Step.id == tool_execution.created_step_id)
                                    )
                                    step = step_result.scalars().first()
                                    if step:
                                        tool_info += f" - Step: '{step.title}'"
                                    else:
                                        tool_info += f" - Step #{tool_execution.created_step_id}"
                                
                                elif tool_execution.result_summary:
                                    # Condense result summary
                                    summary = tool_execution.result_summary
                                    if len(summary) > 60:
                                        summary = summary[:60] + "..."
                                    tool_info += f" - {summary}"
                            
                            elif tool_execution.status == 'error' and tool_execution.error_message:
                                # Show condensed error
                                error = tool_execution.error_message
                                if len(error) > 50:
                                    error = error[:50] + "..."
                                tool_info += f" - Error: {error}"
                            
                            system_parts.append(tool_info)
                
                # If no blocks or content, fall back to completion.completion
                if not system_parts and completion.completion:
                    if isinstance(completion.completion, dict):
                        # Handle JSON completion format
                        content = completion.completion.get('content', '') or completion.completion.get('message', '')
                    else:
                        content = str(completion.completion)
                    
                    if content.strip():
                        system_parts.append(f"Response: {content.strip()}")
                
                if system_parts:
                    conversation.append(f"Assistant ({timestamp}): {' | '.join(system_parts)}")
        
        # Join all conversation parts
        conversation_text = "\n".join(conversation) if conversation else "No conversation history available"
        
        # Only truncate the entire final context if it's too long (like old agent.py approach)
        max_context_length = 8000  # Reasonable limit for LLM context
        if len(conversation_text) > max_context_length:
            conversation_text = conversation_text[:max_context_length] + "...\n[Context truncated due to length]"
        
        return conversation_text

    async def build(
        self,
        max_messages: int = 20,
        role_filter: Optional[List[str]] = None
    ) -> MessagesSection:
        """Build object-based messages section using the same data path as build_context."""
        from app.models.completion_block import CompletionBlock

        items: List[MessageItem] = []

        allow_llm_see_data = True
        if self.organization_settings:
            try:
                settings_dict = self.organization_settings.config
                allow_llm_see_data = settings_dict.get("allow_llm_see_data", {}).get("value", True)
            except Exception:
                allow_llm_see_data = False

        report_completions = await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.asc())
        )
        report_completions = report_completions.scalars().all()

        completions_to_process = (
            report_completions[:-1]
            if report_completions and report_completions[-1].role == 'user'
            else report_completions
        )

        if role_filter:
            completions_to_process = [c for c in completions_to_process if c.role in role_filter]

        completions_to_process = completions_to_process[-max_messages:]

        for completion in completions_to_process:
            ts = completion.created_at.strftime("%H:%M") if getattr(completion, 'created_at', None) else None
            if completion.role == 'user':
                content = completion.prompt.get('content', '') if completion.prompt else ''
                if content and content.strip():
                    items.append(MessageItem(role="user", timestamp=ts, text=content.strip()))
            elif completion.role == 'system':
                # Aggregate blocks like build_context
                blocks_result = await self.db.execute(
                    select(CompletionBlock)
                    .filter(CompletionBlock.completion_id == completion.id)
                    .order_by(CompletionBlock.block_index.asc())
                )
                blocks = blocks_result.scalars().all()
                system_parts: List[str] = []
                for block in blocks:
                    if block.reasoning and block.reasoning.strip():
                        system_parts.append(f"Thinking: {block.reasoning.strip()}")
                    if block.content and block.content.strip():
                        system_parts.append(f"Response: {block.content.strip()}")
                    if block.tool_execution_id:
                        from app.models.tool_execution import ToolExecution
                        tool_result = await self.db.execute(
                            select(ToolExecution).filter(ToolExecution.id == block.tool_execution_id)
                        )
                        tool_execution = tool_result.scalars().first()
                        if tool_execution:
                            tool_info = f"Tool: {tool_execution.tool_name}"
                            if tool_execution.tool_action:
                                tool_info += f" → {tool_execution.tool_action}"
                            tool_info += f" ({tool_execution.status})"
                            if tool_execution.status == 'success' and tool_execution.tool_name == 'create_widget' and tool_execution.result_json:
                                result_json = tool_execution.result_json or {}
                                widget_data = result_json.get('widget_data', {}) or {}
                                columns = widget_data.get('columns', []) or []
                                rows = widget_data.get('rows', []) or []
                                col_names = [c.get('field') or c.get('headerName') for c in columns if (c.get('field') or c.get('headerName'))]
                                row_count = len(rows)
                                digest_parts = [f"{row_count} rows × {len(col_names)} cols"]
                                if col_names:
                                    head_cols = ", ".join(col_names[:3])
                                    digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 3 else ''}")
                                if allow_llm_see_data:
                                    preview = result_json.get('data_preview', {}) or {}
                                    preview_rows = preview.get('rows') or []
                                    sample_row = preview_rows[0] if preview_rows else (rows[0] if rows else None)
                                    if sample_row:
                                        try:
                                            digest_parts.append(f"top row: {json.dumps(sample_row)}")
                                        except Exception:
                                            pass
                                tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'answer_question' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                answer_text = rj.get('answer') or ((rj.get('output') or {}).get('answer') if isinstance(rj.get('output'), dict) else None)
                                if answer_text:
                                    tool_info += f" - AI answer: {answer_text}"
                            elif tool_execution.status == 'error' and tool_execution.error_message:
                                error = tool_execution.error_message
                                if len(error) > 50:
                                    error = error[:50] + "..."
                                tool_info += f" - Error: {error}"
                            system_parts.append(tool_info)
                if not system_parts and completion.completion:
                    if isinstance(completion.completion, dict):
                        content = completion.completion.get('content', '') or completion.completion.get('message', '')
                    else:
                        content = str(completion.completion)
                    if content.strip():
                        system_parts.append(f"Response: {content.strip()}")
                if system_parts:
                    items.append(MessageItem(role="system", timestamp=ts, text=" | ".join(system_parts)))

        return MessagesSection(items=items)
    
    async def get_message_count(self, role_filter: Optional[List[str]] = None) -> int:
        """Get total number of messages for this report."""
        query = select(Completion).filter(Completion.report_id == self.report.id)
        
        if role_filter:
            query = query.filter(Completion.role.in_(role_filter))
            
        result = await self.db.execute(query)
        return len(result.scalars().all())
    
    async def render(self, max_messages: int = 10) -> str:
        """Render a human-readable view of message context."""
        total_count = await self.get_message_count()
        
        parts = [
            f"Message Context: {total_count} total messages",
            "=" * 40
        ]
        
        if total_count == 0:
            parts.append("\nNo messages in conversation")
            return "\n".join(parts)
        
        # Get recent messages
        report_completions = await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.desc())
            .limit(max_messages)
        )
        recent_messages = report_completions.scalars().all()
        
        if recent_messages:
            parts.append(f"\nRecent {len(recent_messages)} messages:")
            for i, msg in enumerate(reversed(recent_messages)):
                timestamp = msg.created_at.strftime("%H:%M:%S")
                content_preview = (
                    msg.prompt['content'][:100] if msg.role == 'user' 
                    else msg.completion['content'][:100]
                )
                parts.append(f"  {i+1}. [{timestamp}] {msg.role}: {content_preview}...")
        
        return "\n".join(parts)