"""
Memory Context Builder - Ports proven logic from agent._build_memories_context()
"""
import pandas as pd
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.mention import Mention, MentionType
from app.models.memory import Memory
from app.models.step import Step


class MemoryContextBuilder:
    """
    Builds memory context for agent execution.
    
    Ports the proven logic from agent._build_memories_context() with
    mention associations, step data, and memory content.
    """
    
    def __init__(self, db: AsyncSession, organization, user, head_completion):
        self.db = db
        self.organization = organization
        self.user = user
        self.head_completion = head_completion
    
    async def build_context(self, max_memories: int = 10) -> str:
        """
        Build comprehensive memory context.
        
        Exact port from agent._build_memories_context() - proven logic.
        
        Args:
            max_memories: Maximum number of memories to include
        
        Returns:
            Formatted memory context string with memory data and step information
        """
        if not self.head_completion:
            return ""
        
        context = []
        
        # Get mentions for memories associated with this completion
        mentions = await self.db.execute(
            select(Mention)
            .where(Mention.type == MentionType.MEMORY)
            .where(Mention.completion_id == self.head_completion.id)
        )
        mentions = mentions.scalars().all()
        
        # Apply max_memories limit
        if len(mentions) > max_memories:
            mentions = mentions[:max_memories]
        
        # Process each memory mention
        for mention in mentions:
            # Get the memory object
            memory_result = await self.db.execute(
                select(Memory).where(Memory.id == mention.object_id)
            )
            memory = memory_result.scalars().first()
            
            if not memory:
                continue
            
            # Get step associated with memory
            step = None
            if memory.step_id:
                step_result = await self.db.execute(
                    select(Step).where(Step.id == memory.step_id)
                )
                step = step_result.scalars().first()
            
            if step:
                data_model = step.data_model
                data_sample = step.data  # step.data is already a dictionary
                code = step.code
                
                # Create DataFrame from the 'rows' data if available
                try:
                    if (data_sample and 
                        isinstance(data_sample, dict) and 
                        'columns' in data_sample and 
                        'rows' in data_sample):
                        
                        df_cols = [col['field'] for col in data_sample['columns']]
                        df = pd.DataFrame(data_sample['rows'], columns=df_cols)
                        
                        # Take the first 3 rows
                        df = df.head(3)
                        
                        # Convert to dict format
                        data_sample_formatted = df.to_dict(orient='records')
                    else:
                        data_sample_formatted = "No data sample available"
                        
                except Exception:
                    data_sample_formatted = "Error formatting data sample"
                
                # Build memory context entry
                memory_entry = (
                    f"memory {memory.title}: \n"
                    f"memory data model: {data_model}\n"
                    f"memory code: {code}\n"
                    f"memory data sample: {data_sample_formatted}"
                )
                
                context.append(memory_entry)
        
        return "\n\n".join(context)
    
    async def get_memory_count(self) -> int:
        """Get total number of memories for this completion."""
        if not self.head_completion:
            return 0
        
        mentions = await self.db.execute(
            select(Mention)
            .where(Mention.type == MentionType.MEMORY)
            .where(Mention.completion_id == self.head_completion.id)
        )
        return len(mentions.scalars().all())
    
    async def render(self, max_memories: int = 5) -> str:
        """Render a human-readable view of memory context."""
        memory_count = await self.get_memory_count()
        
        parts = [
            f"Memory Context: {memory_count} memories available",
            "=" * 42
        ]
        
        if memory_count == 0:
            parts.append("\nNo memories referenced in this completion")
            return "\n".join(parts)
        
        if not self.head_completion:
            parts.append("\nNo head completion available")
            return "\n".join(parts)
        
        # Get mentions for memories
        mentions = await self.db.execute(
            select(Mention)
            .where(Mention.type == MentionType.MEMORY)
            .where(Mention.completion_id == self.head_completion.id)
        )
        mentions = mentions.scalars().all()
        
        # Limit to max_memories
        if len(mentions) > max_memories:
            mentions = mentions[:max_memories]
        
        parts.append(f"\nReferenced memories ({len(mentions)}):")
        for i, mention in enumerate(mentions):
            # Get the memory object
            memory_result = await self.db.execute(
                select(Memory).where(Memory.id == mention.object_id)
            )
            memory = memory_result.scalars().first()
            
            if memory:
                parts.append(f"  {i+1}. {memory.title}")
                if memory.step_id:
                    parts.append(f"     Associated with step ID: {memory.step_id}")
        
        return "\n".join(parts)