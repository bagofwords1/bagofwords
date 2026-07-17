"""Stop Eval Run Tool — tear down an in-progress TestRun.

Thin wrapper over ``TestRunService.stop_run`` (the same path as the UI's
stop button and ``POST /api/tests/runs/{id}/stop``). Use when the user
asks to stop a run, or when a background run is superseded (e.g. the
instructions changed again and the old run's verdict no longer matters).
Stopping a wake-armed run still fires one final wake reporting the
stopped status.
"""
from typing import Any, AsyncIterator, Dict, Type
import logging

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.stop_eval_run import StopEvalRunInput, StopEvalRunOutput
from app.core.permission_resolver import resolve_permissions

logger = logging.getLogger(__name__)


class StopEvalRunTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="stop_eval_run",
            description=(
                "ACTION: Stop an in-progress eval TestRun. In-flight cases are "
                "marked errored ('Stopped by user') and the run finalizes as "
                "'stopped'. Use when the user asks to stop a run or when a "
                "background run is superseded. Already-terminal runs are "
                "returned unchanged. Find run ids with get_eval_runs."
            ),
            category="action",
            version="1.0.0",
            input_schema=StopEvalRunInput.model_json_schema(),
            output_schema=StopEvalRunOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=["manage_evals"],
            tags=["eval", "run", "stop"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"run_id": "<run-uuid>"},
                    "description": "User said 'stop that eval run'",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return StopEvalRunInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return StopEvalRunOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = StopEvalRunInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(type="tool.start", payload={"run_id": data.run_id})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": "Missing required runtime context (db, organization, user)", "code": "MISSING_CONTEXT"},
            )
            return

        try:
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if not resolved.has_org_permission("manage_evals"):
                yield ToolErrorEvent(
                    type="tool.error",
                    payload={"error": "Missing manage_evals permission", "code": "PERMISSION_DENIED"},
                )
                return

            from app.services.test_run_service import TestRunService

            try:
                run = await TestRunService().stop_run(db, str(organization.id), user, str(data.run_id))
            except Exception as stop_err:
                detail = getattr(stop_err, "detail", None) or str(stop_err)
                output = StopEvalRunOutput(
                    success=False,
                    run_id=str(data.run_id),
                    rejected_reason="stop_failed",
                    message=str(detail),
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {"summary": f"stop_eval_run rejected: {detail}", "artifacts": []},
                    },
                )
                return

            summary = dict(run.summary_json or {})
            output = StopEvalRunOutput(
                success=True,
                run_id=str(run.id),
                status=run.status,
                total=int(summary.get("total") or 0),
                passed=int(summary.get("passed") or 0),
                failed=int(summary.get("failed") or 0),
                message=f"Run {run.id} is now '{run.status}'.",
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": output.message,
                        "artifacts": [
                            {"type": "eval_run", "run_id": str(run.id), "status": run.status}
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"stop_eval_run failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Stop failed: {e}", "code": "STOP_FAILED"},
            )
