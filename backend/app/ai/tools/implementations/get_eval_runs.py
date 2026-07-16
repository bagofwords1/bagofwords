"""Get Eval Runs Tool — list recent TestRuns (summaries only).

The cheap half of the eval-read pair: answers "what's running?" and
"what ran recently?" without per-case detail, so a long history stays
small in context. Automation runs (trigger_reason="automation:*") appear
here too. For per-case results or a build-over-build diff, use
``get_eval_run``.
"""
from typing import Any, AsyncIterator, Dict, Type
import logging

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.get_eval_run import (
    EvalRunSummary,
    GetEvalRunsInput,
    GetEvalRunsOutput,
)
from app.core.permission_resolver import resolve_permissions
from app.models.eval import TestCase, TestResult, TestRun, TestSuite

logger = logging.getLogger(__name__)


def _iso(dt) -> str | None:
    try:
        return dt.isoformat() if dt else None
    except Exception:
        return None


def run_counts(run: TestRun, results) -> Dict[str, int]:
    """Counts from summary_json when finalized, live tallies otherwise."""
    summary = run.summary_json or {}
    if summary.get("total"):
        return {
            "total": int(summary.get("total") or 0),
            "passed": int(summary.get("passed") or 0),
            "failed": int(summary.get("failed") or 0),
            "finished": int(summary.get("total") or 0),
        }
    statuses = [getattr(r, "status", "") for r in results]
    terminal = {"pass", "fail", "error", "stopped"}
    return {
        "total": len(statuses),
        "passed": sum(1 for s in statuses if s == "pass"),
        "failed": sum(1 for s in statuses if s in ("fail", "error")),
        "finished": sum(1 for s in statuses if s in terminal),
    }


class GetEvalRunsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="get_eval_runs",
            description=(
                "RESEARCH: List recent eval TestRuns (most recent first) — id, "
                "title, status, trigger, build number, pass/fail counts. Use "
                "status='in_progress' to see currently executing runs. Summary "
                "only; read one run's per-case results with get_eval_run."
            ),
            category="research",
            version="1.0.0",
            input_schema=GetEvalRunsInput.model_json_schema(),
            output_schema=GetEvalRunsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=15,
            idempotent=True,
            required_permissions=["manage_evals"],
            tags=["eval", "run", "list"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"status": "in_progress"},
                    "description": "What eval runs are executing right now?",
                },
                {
                    "input": {"limit": 5},
                    "description": "The last five runs, any status",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GetEvalRunsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GetEvalRunsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = GetEvalRunsInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(type="tool.start", payload={"status": data.status, "limit": data.limit})

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

            # Org-scope via the result → case → suite chain.
            stmt = (
                select(TestRun)
                .join(TestResult, TestResult.run_id == TestRun.id)
                .join(TestCase, TestCase.id == TestResult.case_id)
                .join(TestSuite, TestSuite.id == TestCase.suite_id)
                .where(TestSuite.organization_id == str(organization.id))
                .order_by(TestRun.created_at.desc())
                .distinct()
                .limit(data.limit)
            )
            if data.status != "all":
                stmt = stmt.where(TestRun.status == data.status)
            runs = (await db.execute(stmt)).scalars().all()

            items = []
            for run in runs:
                results = (
                    await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)))
                ).scalars().all()
                counts = run_counts(run, results)
                items.append(
                    EvalRunSummary(
                        run_id=str(run.id),
                        title=run.title,
                        status=run.status,
                        trigger_reason=run.trigger_reason,
                        build_number=getattr(run, "build_number", None),
                        started_at=_iso(run.started_at),
                        finished_at=_iso(run.finished_at),
                        **counts,
                    )
                )

            output = GetEvalRunsOutput(
                success=True,
                items=items,
                total=len(items),
                message=f"Found {len(items)} run(s)" + (f" with status={data.status}" if data.status != "all" else ""),
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": output.message,
                        "artifacts": [
                            {
                                "type": "eval_run_list",
                                "count": len(items),
                                "items": [
                                    {"run_id": i.run_id, "status": i.status, "passed": i.passed, "total": i.total}
                                    for i in items
                                ],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"get_eval_runs failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"List failed: {e}", "code": "LIST_FAILED"},
            )
