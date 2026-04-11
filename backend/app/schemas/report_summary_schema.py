from pydantic import BaseModel
from typing import Optional, List

from .tool_execution_schema import ToolExecutionSchema
from .step_schema import StepSchema
from .completion_v2_schema import ToolExecutionDataSourceSchema


class SummaryToolExecutionSchema(ToolExecutionSchema):
    """Tool execution for summary view — includes hydrated step but no widget."""
    created_step: Optional[StepSchema] = None
    data_sources: Optional[list[ToolExecutionDataSourceSchema]] = None


class SummaryInstructionItem(BaseModel):
    instruction_id: str
    title: str
    category: str
    is_edit: bool
    line_count: int
    message_id: str  # completion_id, for scrollToMessage


class ReportSummaryResponse(BaseModel):
    queries: List[SummaryToolExecutionSchema]
    instructions: List[SummaryInstructionItem]
