"""Run Eval Tool — kick off a TestRun and stream live progress.

Available only in training mode. Refuses to run if the *current* agent
execution is itself an eval run (``runtime_ctx['is_eval_run'] is True``)
to prevent infinite nesting.

The tool blocks the agent loop while the eval run executes — that's
the desired UX for "I want to know if this passes". Progress is
forwarded to the chat client via ``ToolProgressEvent`` payloads (kind
= ``eval.*``), which the existing ``tool.progress`` SSE pipeline
already wires through to the browser.

Sigkill cascade: when the parent system completion is sigkilled (the
chat completion that called this tool), the polling loop detects it
and calls ``TestRunService.stop_run`` so the in-flight TestRun is
torn down.
"""
from typing import Any, AsyncIterator, Dict, Type
import asyncio
import logging
import time

from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.schemas.run_eval import (
    EVAL_CASE_FINISHED,
    EVAL_CASE_STARTED,
    EVAL_RUN_FINISHED,
    EVAL_RUN_STARTED,
    EVAL_RUN_TERMINAL_STATUSES,
    EVAL_TERMINAL_STATUSES,
    RunEvalCaseResult,
    RunEvalInput,
    RunEvalOutput,
)
from app.core.permission_resolver import resolve_permissions
from app.models.completion import Completion
from app.models.eval import (
    TEST_CASE_STATUS_ACTIVE,
    TestCase,
    TestResult,
    TestRun,
)

logger = logging.getLogger(__name__)


# Hard cap so a runaway eval doesn't pin the agent loop indefinitely.
_PER_CASE_TIMEOUT_S = 5 * 60
_MAX_TIMEOUT_S = 30 * 60
_POLL_INTERVAL_S = 1.0


class RunEvalTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="run_eval",
            description=(
                "ACTION: Run one or more eval test cases and stream the result "
                "back to chat. Provide either ``case_ids`` (specific cases — "
                "drafts allowed) or ``suite_id`` (all active cases in the "
                "suite). The user sees pass/fail counts tick up live. Refuses "
                "if invoked from inside an eval run already (no nesting)."
            ),
            category="action",
            version="1.0.0",
            input_schema=RunEvalInput.model_json_schema(),
            output_schema=RunEvalOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=_MAX_TIMEOUT_S,
            idempotent=False,
            required_permissions=["manage_evals"],
            tags=["eval", "run"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"case_ids": ["<case-uuid>"]},
                    "description": "Single-case run after authoring a new eval",
                },
                {
                    "input": {"suite_id": "<suite-uuid>"},
                    "description": "Run a whole suite of active cases",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return RunEvalInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return RunEvalOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = RunEvalInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "case_ids": data.case_ids,
                "suite_id": data.suite_id,
            },
        )

        # --- Recursion guard: refuse if we're already inside an eval run ---
        if bool(runtime_ctx.get("is_eval_run")):
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": RunEvalOutput(
                        success=False,
                        rejected_reason="EVAL_NESTING_FORBIDDEN",
                        message=(
                            "run_eval cannot be called from inside an eval "
                            "execution — would create a recursive TestRun."
                        ),
                    ).model_dump(),
                    "observation": {
                        "summary": "run_eval rejected: nested invocation forbidden",
                        "artifacts": [],
                    },
                },
            )
            return

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        system_completion = runtime_ctx.get("system_completion")
        sigkill_event = runtime_ctx.get("sigkill_event")

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
            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if not resolved.has_org_permission("manage_evals"):
                yield ToolErrorEvent(
                    type="tool.error",
                    payload={"error": "Missing manage_evals permission", "code": "PERMISSION_DENIED"},
                )
                return

            # --- Resolve target cases ---
            from app.services.test_run_service import TestRunService

            run_service = TestRunService()

            target_case_ids: list[str] = []
            target_cases_meta: dict[str, str] = {}  # id -> name

            if data.case_ids:
                ids = [str(c) for c in data.case_ids]
                stmt = (
                    select(TestCase)
                    .where(TestCase.id.in_(ids))
                    .where(TestCase.deleted_at.is_(None))
                )
                rows = (await db.execute(stmt)).scalars().all()
                # Org-scope check via the suite chain.
                from app.models.eval import TestSuite

                for c in rows:
                    suite_stmt = (
                        select(TestSuite.id)
                        .where(TestSuite.id == str(c.suite_id))
                        .where(TestSuite.organization_id == str(organization.id))
                    )
                    if (await db.execute(suite_stmt)).first() is None:
                        continue
                    target_case_ids.append(str(c.id))
                    target_cases_meta[str(c.id)] = c.name
            else:
                # suite_id path — only active cases (drafts are inert in
                # default suite-level runs; if a user wants to run a draft
                # they pass it via case_ids explicitly).
                from app.models.eval import TestSuite

                suite_stmt = (
                    select(TestSuite)
                    .where(TestSuite.id == str(data.suite_id))
                    .where(TestSuite.organization_id == str(organization.id))
                    .where(TestSuite.deleted_at.is_(None))
                )
                suite = (await db.execute(suite_stmt)).scalar_one_or_none()
                if not suite:
                    yield ToolEndEvent(
                        type="tool.end",
                        payload={
                            "output": RunEvalOutput(
                                success=False,
                                rejected_reason="suite_not_found",
                                message=f"Suite {data.suite_id} not found in this organization.",
                            ).model_dump(),
                            "observation": {"summary": "run_eval rejected: suite not found", "artifacts": []},
                        },
                    )
                    return
                cases = await run_service._get_cases(db, str(suite.id), status=TEST_CASE_STATUS_ACTIVE)
                for c in cases:
                    target_case_ids.append(str(c.id))
                    target_cases_meta[str(c.id)] = c.name

            if not target_case_ids:
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": RunEvalOutput(
                            success=False,
                            rejected_reason="no_cases",
                            message="No runnable cases found for the given inputs.",
                        ).model_dump(),
                        "observation": {"summary": "run_eval rejected: no runnable cases", "artifacts": []},
                    },
                )
                return

            total = len(target_case_ids)
            timeout_s = min(total * _PER_CASE_TIMEOUT_S, _MAX_TIMEOUT_S)

            # --- Kick off the TestRun ---
            run, _results = await run_service.create_and_execute_background(
                db=db,
                organization=organization,
                current_user=user,
                case_ids=target_case_ids,
                trigger_reason="agent_run_eval",
            )
            run_id = str(run.id)

            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "kind": EVAL_RUN_STARTED,
                    "run_id": run_id,
                    "total": total,
                    "case_ids": target_case_ids,
                    "case_names": [target_cases_meta.get(cid, "") for cid in target_case_ids],
                    "timeout_s": timeout_s,
                },
            )

            # --- Poll for state transitions ---
            seen_status: dict[str, str] = {}  # case_id -> last seen TestResult.status
            seen_started: set[str] = set()
            final_results: list[RunEvalCaseResult] = []
            run_status = "in_progress"
            stopped_via_sigkill = False
            timed_out = False
            deadline = time.monotonic() + timeout_s

            while True:
                # 1. Sigkill cascade — parent agent's sigkill or completion stop.
                killed = False
                if sigkill_event is not None and sigkill_event.is_set():
                    killed = True
                else:
                    sys_id = getattr(system_completion, "id", None) if system_completion else None
                    if sys_id:
                        try:
                            sys_row = await db.get(Completion, str(sys_id))
                            if sys_row and getattr(sys_row, "status", None) == "stopped":
                                killed = True
                        except Exception:
                            pass

                if killed and not stopped_via_sigkill:
                    stopped_via_sigkill = True
                    try:
                        await run_service.stop_run(db, str(organization.id), user, run_id)
                    except Exception as stop_err:
                        logger.warning(f"run_eval sigkill cascade failed to stop run {run_id}: {stop_err}")
                    # Don't break yet — fall through to one more state read so
                    # we emit terminal events for cases the eval finished
                    # before stop_run wrote its statuses.

                # 2. Read TestResult rows and emit transitions.
                results_stmt = (
                    select(TestResult, TestCase.name)
                    .join(TestCase, TestCase.id == TestResult.case_id)
                    .where(TestResult.run_id == run_id)
                )
                rows = (await db.execute(results_stmt)).all()

                passed_so_far = sum(1 for r, _ in rows if r.status == "pass")
                failed_so_far = sum(1 for r, _ in rows if r.status in ("fail", "error"))
                finished_so_far = sum(
                    1 for r, _ in rows if r.status in EVAL_TERMINAL_STATUSES
                )

                for idx, (result, case_name) in enumerate(rows):
                    cid = str(result.case_id)
                    prev = seen_status.get(cid)
                    cur = result.status

                    if prev != cur:
                        # case_started transition
                        if cid not in seen_started and cur not in EVAL_TERMINAL_STATUSES.union({"init"}):
                            seen_started.add(cid)
                            yield ToolProgressEvent(
                                type="tool.progress",
                                payload={
                                    "kind": EVAL_CASE_STARTED,
                                    "run_id": run_id,
                                    "case_id": cid,
                                    "case_name": case_name,
                                    "index": idx,
                                    "total": total,
                                },
                            )
                        # case_finished transition
                        if cur in EVAL_TERMINAL_STATUSES:
                            yield ToolProgressEvent(
                                type="tool.progress",
                                payload={
                                    "kind": EVAL_CASE_FINISHED,
                                    "run_id": run_id,
                                    "case_id": cid,
                                    "case_name": case_name,
                                    "status": cur,
                                    "failure_reason": getattr(result, "failure_reason", None),
                                    "passed_so_far": passed_so_far,
                                    "failed_so_far": failed_so_far,
                                    "finished_so_far": finished_so_far,
                                    "total": total,
                                },
                            )
                        seen_status[cid] = cur

                # 3. Check run-level terminal status.
                try:
                    await db.refresh(run)
                except Exception:
                    pass
                run_status = getattr(run, "status", "in_progress") or "in_progress"
                if run_status in EVAL_RUN_TERMINAL_STATUSES:
                    break

                # 4. Hard timeout.
                if time.monotonic() >= deadline:
                    timed_out = True
                    try:
                        await run_service.stop_run(db, str(organization.id), user, run_id)
                    except Exception as stop_err:
                        logger.warning(f"run_eval hard-timeout stop failed for run {run_id}: {stop_err}")
                    # Refresh once more to capture stopped state.
                    try:
                        await db.refresh(run)
                    except Exception:
                        pass
                    run_status = getattr(run, "status", run_status) or run_status
                    break

                if killed:
                    # We already cascaded the stop — let the next iteration
                    # capture terminal statuses, but don't loop forever.
                    if run_status in EVAL_RUN_TERMINAL_STATUSES:
                        break

                await asyncio.sleep(_POLL_INTERVAL_S)

            # --- Build final summary ---
            results_stmt = (
                select(TestResult, TestCase.name)
                .join(TestCase, TestCase.id == TestResult.case_id)
                .where(TestResult.run_id == run_id)
            )
            rows = (await db.execute(results_stmt)).all()
            for result, case_name in rows:
                final_results.append(
                    RunEvalCaseResult(
                        case_id=str(result.case_id),
                        case_name=case_name,
                        status=result.status,
                        failure_reason=getattr(result, "failure_reason", None),
                    )
                )
            passed = sum(1 for r in final_results if r.status == "pass")
            failed = sum(1 for r in final_results if r.status in ("fail", "error"))
            finished = sum(1 for r in final_results if r.status in EVAL_TERMINAL_STATUSES)

            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "kind": EVAL_RUN_FINISHED,
                    "run_id": run_id,
                    "status": run_status,
                    "passed": passed,
                    "failed": failed,
                    "finished": finished,
                    "total": total,
                    "stopped_via_sigkill": stopped_via_sigkill,
                    "timed_out": timed_out,
                },
            )

            output = RunEvalOutput(
                success=run_status == "success",
                run_id=run_id,
                status=run_status,
                total=total,
                passed=passed,
                failed=failed,
                finished=finished,
                results=final_results,
                message=(
                    f"Run {run_status}: {passed}/{total} passed, {failed} failed"
                    + (" (stopped)" if stopped_via_sigkill else "")
                    + (" (timed out)" if timed_out else "")
                ),
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": output.message,
                        "stopped": stopped_via_sigkill,
                        "artifacts": [
                            {
                                "type": "eval_run",
                                "run_id": run_id,
                                "status": run_status,
                                "total": total,
                                "passed": passed,
                                "failed": failed,
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"run_eval failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Run failed: {e}", "code": "RUN_FAILED"},
            )
