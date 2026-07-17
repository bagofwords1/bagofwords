"""Run Eval Tool — kick off a TestRun, background-first.

Available only in training mode. Refuses to run if the *current* agent
execution is itself an eval run (``runtime_ctx['is_eval_run'] is True``)
to prevent infinite nesting.

Default behavior (``wait_s=0``): create the run, arm the run-finished
wake-up on the current conversation, and return immediately with the
run_id. The background finalizer in ``TestRunService`` evaluates each
case as its agent finishes, finalizes the run, and fires a completion on
this report — so results arrive without anyone polling. ``get_eval_run``
reads the run on demand at any time.

With ``wait_s > 0`` the tool stays attached up to that budget, streaming
live per-case progress to the chat card (``ToolProgressEvent`` payloads,
kind = ``eval.*``). A heartbeat progress event fires every ~30s so the
agent-level idle timeout never kills a quiet-but-healthy run. If the run
outlives the budget the tool arms the wake-up and detaches — the run
keeps executing server-side.

Sigkill cascade: while attached, if the parent system completion is
sigkilled the polling loop calls ``TestRunService.stop_run`` so the
in-flight TestRun is torn down. After detach, stopping is an explicit
action (``stop_eval_run`` tool or the UI stop button).
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
    EVAL_HEARTBEAT,
    EVAL_RUN_DETACHED,
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

_POLL_INTERVAL_S = 1.0
_HEARTBEAT_INTERVAL_S = 30.0


class RunEvalTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="run_eval",
            description=(
                "ACTION: Start an eval TestRun for ``case_ids`` (specific cases — "
                "drafts allowed) or ``suite_id`` (all active cases in the suite). "
                "Runs in the BACKGROUND by default: returns immediately with the "
                "run_id, and a wake-up message arrives in this conversation when "
                "the run finishes — do NOT arm a `wait` for the same run. Check "
                "progress or results any time with get_eval_run. Set ``wait_s`` "
                "(e.g. 60-120) only for a quick check the user is actively "
                "waiting on; if the run outlives the budget it detaches and "
                "keeps executing. An identical already-running run is reused "
                "(``deduped=true``) instead of duplicated. Refuses if invoked "
                "from inside an eval run already (no nesting)."
            ),
            category="action",
            version="2.0.0",
            input_schema=RunEvalInput.model_json_schema(),
            output_schema=RunEvalOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=660,
            idempotent=False,
            required_permissions=["manage_evals"],
            tags=["eval", "run"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"case_ids": ["<case-uuid>"]},
                    "description": "Background run after authoring a new eval — results arrive via wake-up",
                },
                {
                    "input": {"case_ids": ["<case-uuid>"], "wait_s": 120},
                    "description": "Stay attached up to 2 minutes for a quick single-case check",
                },
                {
                    "input": {"suite_id": "<suite-uuid>"},
                    "description": "Run a whole suite of active cases in the background",
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
                "wait_s": data.wait_s,
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
        report = runtime_ctx.get("report")
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

            # --- Pin the candidate build so evals test the *staged* hunks ---
            # Resolution order, most-specific first:
            #   1. ``data.build_id`` — an explicit suggestion build chosen by the
            #      caller. This is how "run eval on Change 1" targets exactly
            #      that suggestion's snapshot (main + only its hunks), unaffected
            #      by any sibling suggestion on the same instruction.
            #   2. ``runtime_ctx['training_build_id']`` — the draft the agent is
            #      accumulating in training / knowledge mode.
            #   3. ``None`` — fall back to the current main build.
            # Without (1)/(2) the run would silently measure MAIN and never
            # exercise the just-authored instructions.
            candidate_build_id = data.build_id or runtime_ctx.get("training_build_id")

            detach_immediately = data.wait_s <= 0

            # --- Kick off the TestRun ---
            # Wake-on-finish is armed at creation only for the immediate-detach
            # path; the attached path arms it at detach time so an inline
            # completion never produces a redundant wake.
            try:
                run, _results = await run_service.create_and_execute_background(
                    db=db,
                    organization=organization,
                    current_user=user,
                    case_ids=target_case_ids,
                    trigger_reason="agent_run_eval",
                    build_id=candidate_build_id,
                    origin_report_id=str(report.id) if report is not None else None,
                    origin_user_id=str(user.id),
                    wake_on_finish=bool(detach_immediately and report is not None),
                )
            except Exception as create_err:
                detail = getattr(create_err, "detail", None) or str(create_err)
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": RunEvalOutput(
                            success=False,
                            rejected_reason="run_not_started",
                            message=str(detail),
                        ).model_dump(),
                        "observation": {"summary": f"run_eval rejected: {detail}", "artifacts": []},
                    },
                )
                return
            run_id = str(run.id)
            deduped = bool(getattr(run, "deduped", False))

            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "kind": EVAL_RUN_STARTED,
                    "run_id": run_id,
                    "total": total,
                    "case_ids": target_case_ids,
                    "case_names": [target_cases_meta.get(cid, "") for cid in target_case_ids],
                    "wait_s": data.wait_s,
                    "deduped": deduped,
                    "timing": False,
                },
            )

            if detach_immediately:
                output = self._detached_output(
                    run_id=run_id,
                    total=total,
                    target_cases_meta=target_cases_meta,
                    target_case_ids=target_case_ids,
                    deduped=deduped,
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
                                    "run_id": run_id,
                                    "status": "in_progress",
                                    "total": total,
                                    "detached": True,
                                }
                            ],
                        },
                    },
                )
                return

            # --- Attached mode: poll for state transitions up to wait_s ---
            seen_status: dict[str, str] = {}  # case_id -> last seen TestResult.status
            seen_started: set[str] = set()
            final_results: list[RunEvalCaseResult] = []
            run_status = "in_progress"
            stopped_via_sigkill = False
            detached = False
            deadline = time.monotonic() + data.wait_s
            last_heartbeat = time.monotonic()
            passed_so_far = failed_so_far = finished_so_far = 0

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
                    .execution_options(populate_existing=True)
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
                                    "timing": False,
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
                                    "timing": False,
                                },
                            )
                        seen_status[cid] = cur
                        last_heartbeat = time.monotonic()

                # 3. Check run-level terminal status.
                try:
                    await db.refresh(run)
                except Exception:
                    pass
                run_status = getattr(run, "status", "in_progress") or "in_progress"
                if run_status in EVAL_RUN_TERMINAL_STATUSES:
                    break

                # 4. Wait budget exhausted → detach; the run keeps executing.
                if time.monotonic() >= deadline:
                    detached = True
                    try:
                        # Arm wake-on-finish now that results won't be inline.
                        if report is not None:
                            run.origin_report_id = str(report.id)
                            run.origin_user_id = str(user.id)
                            run.wake_on_finish = True
                            db.add(run)
                            await db.commit()
                            await db.refresh(run)
                    except Exception as arm_err:
                        logger.warning(f"run_eval failed to arm wake for {run_id}: {arm_err}")
                    # Re-check: the run may have gone terminal while arming.
                    run_status = getattr(run, "status", run_status) or run_status
                    if run_status in EVAL_RUN_TERMINAL_STATUSES:
                        detached = False
                    break

                if killed:
                    # We already cascaded the stop — let the next iteration
                    # capture terminal statuses, but don't loop forever.
                    if run_status in EVAL_RUN_TERMINAL_STATUSES:
                        break

                # 5. Heartbeat so the tool-runner idle timeout never fires on a
                #    quiet-but-healthy run (idle window is 180s; beat at 30s).
                if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL_S:
                    last_heartbeat = time.monotonic()
                    yield ToolProgressEvent(
                        type="tool.progress",
                        payload={
                            "kind": EVAL_HEARTBEAT,
                            "run_id": run_id,
                            "finished_so_far": finished_so_far,
                            "passed_so_far": passed_so_far,
                            "failed_so_far": failed_so_far,
                            "total": total,
                            "timing": False,
                        },
                    )

                await asyncio.sleep(_POLL_INTERVAL_S)

            if detached:
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={
                        "kind": EVAL_RUN_DETACHED,
                        "run_id": run_id,
                        "finished_so_far": finished_so_far,
                        "passed_so_far": passed_so_far,
                        "failed_so_far": failed_so_far,
                        "total": total,
                        "timing": False,
                    },
                )
                output = self._detached_output(
                    run_id=run_id,
                    total=total,
                    target_cases_meta=target_cases_meta,
                    target_case_ids=target_case_ids,
                    deduped=deduped,
                    finished=finished_so_far,
                    passed=passed_so_far,
                    failed=failed_so_far,
                    waited_s=data.wait_s,
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
                                    "run_id": run_id,
                                    "status": "in_progress",
                                    "total": total,
                                    "detached": True,
                                }
                            ],
                        },
                    },
                )
                return

            # --- Terminal within the budget: build final summary ---
            results_stmt = (
                select(TestResult, TestCase.name)
                .join(TestCase, TestCase.id == TestResult.case_id)
                .where(TestResult.run_id == run_id)
                .execution_options(populate_existing=True)
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

            # Results were delivered inline — make sure no wake fires later.
            try:
                if getattr(run, "wake_on_finish", False):
                    run.wake_on_finish = False
                    db.add(run)
                    await db.commit()
            except Exception:
                pass

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
                    "timing": False,
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
                deduped=deduped,
                message=(
                    f"Run {run_status}: {passed}/{total} passed, {failed} failed"
                    + (" (stopped)" if stopped_via_sigkill else "")
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

    @staticmethod
    def _detached_output(
        *,
        run_id: str,
        total: int,
        target_cases_meta: dict,
        target_case_ids: list,
        deduped: bool,
        finished: int = 0,
        passed: int = 0,
        failed: int = 0,
        waited_s: int = 0,
    ) -> RunEvalOutput:
        results = [
            RunEvalCaseResult(
                case_id=cid,
                case_name=target_cases_meta.get(cid, ""),
                status="in_progress",
            )
            for cid in target_case_ids
        ]
        prefix = "Reusing already-running run" if deduped else "Run"
        waited = f" after waiting {waited_s}s" if waited_s else ""
        return RunEvalOutput(
            success=True,
            run_id=run_id,
            status="in_progress",
            total=total,
            passed=passed,
            failed=failed,
            finished=finished,
            results=results,
            detached=True,
            deduped=deduped,
            message=(
                f"{prefix} {run_id} executing in background ({finished}/{total} cases done{waited}). "
                f"A wake-up message will arrive in this conversation when it finishes — do not arm a "
                f"wait for it. Check progress or results any time with get_eval_run(run_id=...)."
            ),
        )
