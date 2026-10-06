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
    build_id: Optional[str] = None  # draft build this change was staged in


class SummaryListSubmission(BaseModel):
    tool_execution_id: str
    message_id: str  # completion_id, for scrollToMessage


class SummaryListItem(BaseModel):
    """One Agent List the conversation wrote to, across all its submissions.

    Counts are DISTINCT rows: a row added by one call and updated by a later
    one counts once, as added; a row updated twice counts once.
    """
    list_id: str
    list_name: str
    data_source_id: str
    agent_name: Optional[str] = None
    inserted: int = 0
    updated: int = 0
    submissions: List[SummaryListSubmission] = []


class PendingTrainingBuildSchema(BaseModel):
    id: str
    status: str
    total_instructions: int


class ReportSummaryResponse(BaseModel):
    queries: List[SummaryToolExecutionSchema]
    instructions: List[SummaryInstructionItem]
    lists: List[SummaryListItem] = []
    pending_training_build: Optional[PendingTrainingBuildSchema] = None
