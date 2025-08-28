from pydantic import BaseModel
from typing import List, Optional

from .agent_execution_schema import AgentExecutionSchema, ContextSnapshotSchema
from .completion_v2_schema import CompletionBlockV2Schema


class AgentExecutionTraceResponse(BaseModel):
    agent_execution: AgentExecutionSchema
    completion_blocks: List[CompletionBlockV2Schema]
    head_prompt_snippet: Optional[str] = None
    head_context_snapshot: Optional[ContextSnapshotSchema] = None


