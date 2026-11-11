"""MCP adapters for the existing, agent-authorized eval lifecycle.

Keep validation, case/run visibility, and side effects in the internal tools.
MCP has no training report or wake consumer: callers select agent scopes
explicitly and retrieve background results with get_eval_run.
"""
from app.ai.tools.mcp.base import MCPTool
from app.ai.tools.implementations.search_evals import SearchEvalsTool
from app.ai.tools.implementations.create_eval import CreateEvalTool
from app.ai.tools.implementations.edit_eval import EditEvalTool
from app.ai.tools.implementations.run_eval import RunEvalTool
from app.ai.tools.implementations.stop_eval_run import StopEvalRunTool
from app.ai.tools.implementations.get_eval_runs import GetEvalRunsTool
from app.ai.tools.implementations.get_eval_run import GetEvalRunTool


class EvalMCPTool(MCPTool):
    tool_class = None

    @property
    def name(self):
        return self.tool_class().name

    @property
    def description(self):
        return self.tool_class().description + (
            " MCP uses the same agent eval-management permissions as the web app. "
            "Supply agent data_source_ids explicitly when creating scoped cases; "
            "unscoped cases require org-level manage_evals. "
            "Background results are available through get_eval_run."
        )

    @property
    def required_ds_permission(self):
        return "manage_evals"

    @property
    def input_schema(self):
        return self.tool_class().input_model.model_json_schema()

    async def execute(self, args, db, user, organization):
        runtime = {"db": db, "user": user, "organization": organization, "mode": "training"}
        result = None
        async for event in self.tool_class().run_stream(args, runtime):
            if event.type == "tool.error":
                return {"success": False, "error_message": event.payload.get("error"),
                        "code": event.payload.get("code")}
            if event.type == "tool.end":
                result = event.payload.get("output")
        if result is None:
            raise RuntimeError(f"{self.name} completed without an output")
        return result


class SearchEvalsMCPTool(EvalMCPTool):
    tool_class = SearchEvalsTool
    read_only_hint = True


class CreateEvalMCPTool(EvalMCPTool):
    tool_class = CreateEvalTool
    read_only_hint = False


class EditEvalMCPTool(EvalMCPTool):
    tool_class = EditEvalTool
    read_only_hint = False


class RunEvalMCPTool(EvalMCPTool):
    tool_class = RunEvalTool
    read_only_hint = False

    @property
    def description(self):
        return (
            "Run selected eval cases or the active cases in a suite. Requires eval-management "
            "authority over every agent each case targets; global cases require org-level "
            "manage_evals. Returns a background run_id by default. Poll get_eval_run for "
            "status and results, or stop_eval_run to cancel. No chat wake-up is sent."
        )

    @property
    def input_schema(self):
        schema = super().input_schema
        schema["properties"]["wait_s"]["description"] = (
            "Seconds to wait for results (0-600). Default 0 returns a background run_id "
            "immediately. Retrieve results with get_eval_run when detached."
        )
        return schema


class StopEvalRunMCPTool(EvalMCPTool):
    tool_class = StopEvalRunTool
    read_only_hint = False


class GetEvalRunsMCPTool(EvalMCPTool):
    tool_class = GetEvalRunsTool
    read_only_hint = True


class GetEvalRunMCPTool(EvalMCPTool):
    tool_class = GetEvalRunTool
    read_only_hint = True
