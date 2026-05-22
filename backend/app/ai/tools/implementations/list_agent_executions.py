"""List Agent Executions Tool — query historical agent runs with filters.

Available in training mode so an agent can inspect which queries are failing,
getting negative feedback, or have low confidence before writing instructions.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.list_agent_executions import (
    ListAgentExecutionsInput,
    ListAgentExecutionsOutput,
    AgentExecutionItem,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


class ListAgentExecutionsTool(Tool):
    """List historical agent executions with optional issue filters."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_agent_executions",
            description=(
                "ANSWER: Query this platform's own agent execution history — NOT the "
                "user's business database. This tool has no data source; it reads internal "
                "platform logs. Use it as a direct answer (no prior research step needed) "
                "whenever the request is about: agent runs, AI responses, model outputs, "
                "low-confidence responses, bad answers, failed queries, negative feedback, "
                "instruction gaps, or anything about how the AI has been performing. "
                "\n\nEach returned item includes:"
                "\n- prompt: the user's original question/request (what they typed)"
                "\n- status: 'success' or 'error' (did the run complete without tool failures)"
                "\n- step_titles: the data steps / widgets the agent produced as its response"
                "\n- total_tools / total_successful_tools / total_failed_tools: tool call counts"
                "\n- feedback_direction: 1 = positive thumbs-up, -1 = negative thumbs-down, null = none"
                "\n- feedback_message: the user's written feedback comment (if any)"
                "\n- report_name: the report the run happened in"
                "\n- user_name: who triggered the run"
                "\n- created_at: timestamp"
                "\n\nAvailable filters (pass as the 'filter' param):"
                "\n- 'low_confidence' — AI responses scored low (< 3/5)"
                "\n- 'negative_feedback' — runs the user gave a thumbs-down"
                "\n- 'failed_queries' — runs where a data query tool errored"
                "\n- 'low_instruction_coverage' — runs where instructions were ineffective"
                "\n\nAdditional filters:"
                "\n- tool_name — filter to executions that invoked a specific tool, e.g. 'create_data' "
                "(SQL/Python runs that produced data), 'create_artifact' (text/markdown widgets), "
                "'create_dashboard', 'read_query'. Use to find 'all runs that created data' or "
                "'all runs that built a dashboard'."
                "\n- prompt_search — case-insensitive keyword search on the user's prompt text, "
                "e.g. 'revenue' finds all executions where the user asked about revenue."
                "\n\nLeave filter null to return all runs. Use start_date/end_date to scope."
            ),
            category="research",
            version="1.0.0",
            input_schema=ListAgentExecutionsInput.model_json_schema(),
            output_schema=ListAgentExecutionsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=[],
            tags=["agent_execution", "diagnosis", "monitoring", "training"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"filter": "negative_feedback", "page_size": 20},
                    "description": "Find recent runs with negative user feedback",
                },
                {
                    "input": {"filter": "failed_queries", "start_date": "2026-01-01"},
                    "description": "Find failed query runs since the start of the year",
                },
                {
                    "input": {"filter": "low_instruction_coverage", "page_size": 30},
                    "description": "Find runs where instructions weren't effective",
                },
                {
                    "input": {"tool_name": "create_data", "page_size": 20},
                    "description": "List all runs that created data (SQL/Python queries)",
                },
                {
                    "input": {"tool_name": "create_artifact", "page_size": 20},
                    "description": "List all runs that produced text/markdown widgets",
                },
                {
                    "input": {"prompt_search": "revenue", "page_size": 20},
                    "description": "Find runs where the user asked about revenue",
                },
                {
                    "input": {"prompt_search": "top customers", "filter": "negative_feedback"},
                    "description": "Find negatively-rated runs about top customers",
                },
                {
                    "input": {"page": 1, "page_size": 50},
                    "description": "List all recent executions (no filter)",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListAgentExecutionsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListAgentExecutionsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = ListAgentExecutionsInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "filter": data.filter,
                "tool_name": data.tool_name,
                "prompt_search": data.prompt_search,
                "start_date": data.start_date,
                "end_date": data.end_date,
                "page": data.page,
                "page_size": data.page_size,
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        current_report = runtime_ctx.get("report")
        current_report_id = str(getattr(current_report, "id", "") or "") if current_report else None

        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization, user)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            from datetime import datetime
            from app.schemas.console_schema import MetricsQueryParams
            from app.services.console_service import ConsoleService

            params = MetricsQueryParams(
                start_date=datetime.fromisoformat(data.start_date) if data.start_date else None,
                end_date=datetime.fromisoformat(data.end_date) if data.end_date else None,
                data_source_ids=",".join(data.data_source_ids) if data.data_source_ids else None,
            )

            service = ConsoleService()
            result = await service.get_agent_execution_summaries(
                db=db,
                organization=organization,
                params=params,
                page=data.page,
                page_size=data.page_size,
                issue_filter=data.filter,
                tool_name=data.tool_name,
                prompt_search=data.prompt_search,
            )

            items = result.items if hasattr(result, "items") else []
            total = result.total_items if hasattr(result, "total_items") else len(items)

            # Exclude executions from the current report (the one the training agent is running in)
            if current_report_id:
                items = [it for it in items if str(it.report_id) != current_report_id]

            executions = [
                AgentExecutionItem(
                    agent_execution_id=str(it.agent_execution_id),
                    completion_id=it.completion_id,
                    prompt=it.prompt,
                    status=it.agent_execution_status,
                    feedback_direction=it.feedback_direction,
                    feedback_message=it.feedback_message,
                    total_tools=it.total_tools,
                    total_failed_tools=it.total_failed_tools,
                    total_successful_tools=it.total_successful_tools,
                    step_titles=it.step_titles or [],
                    tool_names=it.tool_names or [],
                    report_id=str(it.report_id),
                    report_name=it.report_name,
                    user_name=it.user_name,
                    created_at=it.created_at.isoformat() if hasattr(it.created_at, "isoformat") else str(it.created_at),
                )
                for it in items
            ]

            output = ListAgentExecutionsOutput(
                success=True,
                executions=executions,
                total=total,
                message=f"Found {len(executions)} execution(s) (total: {total})",
            )

            summary = (
                f"Listed {len(executions)} agent execution(s) "
                f"(filter='{data.filter or 'all'}', total={total})"
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "agent_execution_list",
                                "count": len(executions),
                                "total": total,
                                "filter": data.filter,
                            }
                        ],
                    },
                },
            )

        except Exception as e:
            logger.exception(f"list_agent_executions failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Query failed: {e}",
                    "code": "QUERY_FAILED",
                },
            )
