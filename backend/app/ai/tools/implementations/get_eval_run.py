"""Get Eval Run Tool — read one TestRun's status and per-case results.

The detail half of the eval-read pair. Idempotent snapshot: safe to call
while a run is executing (compare finished/total across calls) and after
it finishes. With ``compare_to_previous=true`` it also diffs against the
most recent prior terminal run sharing at least one case — the one-call
answer to "did my instruction change fix it?".
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
    GetEvalRunInput,
    GetEvalRunOutput,
)
from app.ai.tools.schemas.run_eval import RunEvalCaseResult
from app.ai.tools.implementations.get_eval_runs import _iso, run_counts
from app.core.permission_resolver import resolve_permissions
from app.models.eval import TestCase, TestResult, TestRun, TestSuite

logger = logging.getLogger(__name__)


class GetEvalRunTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="get_eval_run",
            description=(
                "RESEARCH: Read one eval TestRun — status, build, counts, and "
                "per-case pass/fail with failure reasons. Safe while the run is "
                "still executing (returns a live snapshot). Set "
                "compare_to_previous=true to also get fixed/regressed flips vs. "
                "the previous comparable run. Find run ids with get_eval_runs "
                "or from run_eval's output."
            ),
            category="research",
            version="1.0.0",
            input_schema=GetEvalRunInput.model_json_schema(),
            output_schema=GetEvalRunOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=["manage_evals"],
            tags=["eval", "run", "read"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"run_id": "<run-uuid>"},
                    "description": "Read results (or live progress) of a run",
                },
                {
                    "input": {"run_id": "<run-uuid>", "compare_to_previous": True},
                    "description": "Did the new instruction build fix the failing cases?",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GetEvalRunInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GetEvalRunOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = GetEvalRunInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"run_id": data.run_id, "compare_to_previous": data.compare_to_previous},
        )

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

            run = (
                await db.execute(
                    select(TestRun).where(TestRun.id == str(data.run_id)).execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            in_org = False
            if run is not None:
                sids = [s for s in (run.suite_ids or "").split(",") if s]
                if sids:
                    in_org = (
                        await db.execute(
                            select(TestSuite.id)
                            .where(TestSuite.id == sids[0])
                            .where(TestSuite.organization_id == str(organization.id))
                        )
                    ).first() is not None
            if run is None or not in_org:
                output = GetEvalRunOutput(
                    success=False,
                    rejected_reason="run_not_found",
                    message=f"Run {data.run_id} not found in this organization.",
                )
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {"summary": output.message, "artifacts": []},
                    },
                )
                return

            rows = (
                await db.execute(
                    select(TestResult, TestCase.name)
                    .join(TestCase, TestCase.id == TestResult.case_id)
                    .where(TestResult.run_id == str(run.id))
                    .execution_options(populate_existing=True)
                )
            ).all()
            results = [
                RunEvalCaseResult(
                    case_id=str(r.case_id),
                    case_name=name,
                    status=r.status,
                    failure_reason=getattr(r, "failure_reason", None),
                )
                for r, name in rows
            ]
            counts = run_counts(run, [r for r, _ in rows])

            compare = None
            if data.compare_to_previous:
                from app.services.test_run_service import TestRunService

                cmp_full = await TestRunService().compare_runs(
                    db, str(organization.id), user, str(run.id)
                )
                if cmp_full.get("against_run"):
                    flips = [c for c in cmp_full.get("cases", []) if c.get("flip") != "same"]
                    compare = {
                        "against_run": cmp_full["against_run"],
                        "summary": cmp_full.get("summary"),
                        "flips": flips,
                    }

            output = GetEvalRunOutput(
                success=True,
                run_id=str(run.id),
                title=run.title,
                status=run.status,
                trigger_reason=run.trigger_reason,
                build_number=getattr(run, "build_number", None),
                started_at=_iso(run.started_at),
                finished_at=_iso(run.finished_at),
                results=results,
                compare=compare,
                message=(
                    f"Run {run.status}: {counts['passed']}/{counts['total']} passed, "
                    f"{counts['failed']} failed ({counts['finished']}/{counts['total']} finished)"
                ),
                **counts,
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": output.message,
                        "artifacts": [
                            {
                                "type": "eval_run",
                                "run_id": str(run.id),
                                "status": run.status,
                                "total": counts["total"],
                                "passed": counts["passed"],
                                "failed": counts["failed"],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"get_eval_run failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Read failed: {e}", "code": "READ_FAILED"},
            )
