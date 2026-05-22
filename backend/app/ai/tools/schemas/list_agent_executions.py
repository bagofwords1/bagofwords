from typing import List, Optional
from pydantic import BaseModel, Field


class ListAgentExecutionsInput(BaseModel):
    """Input schema for list_agent_executions tool."""

    filter: Optional[str] = Field(
        None,
        description=(
            "Narrow results by issue type. Supported values: "
            "'negative_feedback' — runs that received negative user feedback; "
            "'failed_queries' — runs where a data-query tool failed (create_data errors); "
            "'low_confidence' — runs where the response score was < 3/5; "
            "'low_instruction_coverage' — runs where instruction effectiveness was < 3/5. "
            "Omit or pass null to return all executions."
        ),
    )
    start_date: Optional[str] = Field(
        None,
        description="ISO 8601 date string (e.g. '2026-01-01') — include executions on or after this date.",
    )
    end_date: Optional[str] = Field(
        None,
        description="ISO 8601 date string (e.g. '2026-05-22') — include executions on or before this date.",
    )
    tool_name: Optional[str] = Field(
        None,
        description=(
            "Filter to executions that invoked a specific tool. "
            "Examples: 'create_data' (SQL/Python queries), 'create_artifact' (text widgets), "
            "'create_dashboard', 'read_query'. Leave null to include all."
        ),
    )
    prompt_search: Optional[str] = Field(
        None,
        description=(
            "Case-insensitive keyword search against the user's original prompt text. "
            "Use to find executions about a specific topic, e.g. 'revenue' or 'top customers'."
        ),
    )
    data_source_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Limit to executions from reports that use these data source IDs. "
            "Useful when diagnosing issues with a specific data source."
        ),
    )
    page: int = Field(1, description="Page number (1-based).", ge=1)
    page_size: int = Field(
        20,
        description="Results per page (1–50).",
        ge=1,
        le=50,
    )


class AgentExecutionItem(BaseModel):
    """A single agent execution summary."""

    agent_execution_id: str
    completion_id: Optional[str] = None
    prompt: Optional[str] = None
    status: str
    feedback_direction: Optional[int] = None
    feedback_message: Optional[str] = None
    total_tools: int
    total_failed_tools: int
    total_successful_tools: int
    step_titles: List[str] = []
    tool_names: List[str] = []
    report_id: str
    report_name: Optional[str] = None
    user_name: Optional[str] = None
    created_at: str


class ListAgentExecutionsOutput(BaseModel):
    """Output schema for list_agent_executions tool response."""

    success: bool = Field(..., description="Whether the query succeeded.")
    executions: List[AgentExecutionItem] = Field(
        default_factory=list,
        description="Agent execution summaries matching the requested filter.",
    )
    total: int = Field(
        0,
        description="Total matching executions (may exceed the returned page).",
    )
    message: Optional[str] = Field(None, description="Status or error message.")
