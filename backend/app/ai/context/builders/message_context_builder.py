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


class MessageContextBuilder:
    """
    Builds conversation message context for agent execution.
    
    Ports the proven logic from agent._build_messages_context() with
    completion history, widget associations, and step information.
    """
    
    def __init__(self, db: AsyncSession, report):
        self.db = db
        self.report = report
    
    async def build_context(
        self,
        max_messages: int = 20,
        role_filter: Optional[List[str]] = None
    ) -> str:
        """
        Build comprehensive message context.
        
        Exact port from agent._build_messages_context() - proven logic.
        
        Args:
            max_messages: Maximum number of messages to include
            role_filter: Filter by specific roles (e.g., ['user', 'assistant'])
        
        Returns:
            Formatted message context string with conversation history
        """
        context = []
        
        # Get all completions for this report
        report_completions = await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.asc())
        )
        report_completions = report_completions.scalars().all()
        
        # Skip the last completion if it's from a user
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
        
        # Apply max_messages limit
        if len(completions_to_process) > max_messages:
            completions_to_process = completions_to_process[-max_messages:]
        
        # Process each completion
        for completion in completions_to_process:
            # Get widget if exists
            widget = None
            step = None
            
            if completion.widget_id:
                widget_result = await self.db.execute(
                    select(Widget).filter(Widget.id == completion.widget_id)
                )
                widget = widget_result.scalars().first()
            
            # Get step if exists
            if completion.step_id:
                step_result = await self.db.execute(
                    select(Step).filter(Step.id == completion.step_id)
                )
                step = step_result.scalars().first()
            
            # Format each message in a more structured way
            message = {
                "role": completion.role,
                "timestamp": completion.created_at.isoformat(),
                "content": (
                    completion.prompt['content'] 
                    if completion.role == 'user' 
                    else completion.completion['content']
                ),
                "widget": widget.title if widget else None,
                "step": {
                    "title": step.title if step else None,
                    "code": step.code if step else None,
                    "data_model": step.data_model if step else None
                } if step else None
            }
            
            # Convert to a clean, readable format
            context_parts = [
                f"[Message #{len(context) + 1}]",
                f"Role: {message['role']}",
                f"Time: {message['timestamp']}",
                f"Widget: {message['widget'] or 'None'}"
            ]
            
            # Add step information if available
            if message['step']:
                context_parts.extend([
                    "\nWidget Step Information:",
                    "\nData Model:",
                    (
                        json.dumps(message['step']['data_model'], indent=2)
                        if message['step']['data_model'] 
                        else "None"
                    ),
                    "\nCode:",
                    message['step']['code'] if message['step']['code'] else "None"
                ])
            
            # Add content
            context_parts.extend([
                "\nContent:",
                message['content'],
                "\n---"
            ])
            
            context.append("\n".join(context_parts))
        
        return "\n\n".join(context)
    
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