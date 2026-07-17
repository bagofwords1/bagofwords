from typing import Optional, List, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException
from datetime import datetime
import asyncio
import logging
import uuid


def _agent_metadata_from_execution(ae) -> Dict[str, Any]:
    """Extract eval-visible metadata from an AgentExecution row.

    Pulls durations directly and token counts from token_usage_json when
    present. Counts plan decisions via the lazy ``plan_decisions``
    relationship for total_iterations.
    """
    if ae is None:
        return {}
    usage = getattr(ae, "token_usage_json", None) or {}
    if not isinstance(usage, dict):
        usage = {}

    def _pick(*keys):
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                return v
        return None

    return {
        "total_duration_ms": getattr(ae, "total_duration_ms", None),
        "first_token_ms": getattr(ae, "first_token_ms", None),
        "thinking_ms": getattr(ae, "thinking_ms", None),
        "input_tokens": _pick("input_tokens", "prompt_tokens"),
        "output_tokens": _pick("output_tokens", "completion_tokens"),
        "total_tokens": _pick("total_tokens"),
    }

from app.models.eval import TestSuite, TestCase, TestRun, TestResult
from app.models.report import Report
from app.services.report_service import ReportService
from app.models.completion import Completion
from app.services.completion_service import CompletionService
from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
from app.schemas.test_dashboard_schema import TestMetricsSchema, TestSuiteSummarySchema
from app.streaming.completion_stream import CompletionEventQueue
from app.settings.database import create_async_session_factory
from app.ai.agent_v2 import AgentV2
from app.models.agent_execution import AgentExecution
from app.services.test_evaluation_service import TestEvaluationService
from app.ai.agents.judge.judge import Judge
from app.schemas.test_results_schema import TestResultTotals, TestResultJsonSchema, RuleSpec
from app.models.organization import Organization


# Terminal states, shared by the background finalizer and the SSE streamer.
RESULT_TERMINAL_STATUSES = {"pass", "fail", "error", "stopped"}
RUN_TERMINAL_STATUSES = {"success", "error", "stopped"}

# Default org-level cap on simultaneously executing eval runs. Overridable
# per org via OrganizationSettings.config['max_concurrent_eval_runs'].
DEFAULT_MAX_CONCURRENT_EVAL_RUNS = 3

# How long the background finalizer waits for a single case's agent turns
# before marking the result as errored (seconds).
CASE_WATCH_TIMEOUT_S = 15 * 60


class TestRunService:
    def __init__(self) -> None:
        self.completions = CompletionService()
        self.evaluator = TestEvaluationService()

    # -------- Helpers --------

    async def _save_run_summary(self, db: AsyncSession, run: TestRun, results: List[TestResult]) -> None:
        """Save summary_json with pass/fail counts when a test run completes."""
        passed = sum(1 for r in results if r.status == 'pass')
        failed = sum(1 for r in results if r.status in ('fail', 'error'))
        run.summary_json = {'total': len(results), 'passed': passed, 'failed': failed}
        db.add(run)
        await db.commit()
    
    async def _resolve_cases_inputs(self, db: AsyncSession, organization_id: str, case_ids: Optional[List[str]], suite_id: Optional[str]) -> List[TestCase]:
        if case_ids and len(case_ids) > 0:
            res = await db.execute(select(TestCase).where(TestCase.id.in_([str(c) for c in case_ids])))
            cases: List[TestCase] = res.scalars().all()
            if not cases:
                raise HTTPException(status_code=400, detail="No test cases found")
            # Validate suites belong to org
            for c in cases:
                _ = await self._get_suite(db, str(organization_id), str(c.suite_id))
            return cases
        if suite_id:
            _ = await self._get_suite(db, str(organization_id), str(suite_id))
            return await self._get_cases(db, str(suite_id))
        raise HTTPException(status_code=400, detail="Provide case_ids or suite_id")

    async def _get_suite(self, db: AsyncSession, organization_id: str, suite_id: str) -> TestSuite:
        res = await db.execute(select(TestSuite).where(TestSuite.id == suite_id, TestSuite.organization_id == str(organization_id)))
        suite = res.scalar_one_or_none()
        if not suite:
            raise HTTPException(status_code=404, detail="Test suite not found")
        return suite

    async def _get_cases(
        self,
        db: AsyncSession,
        suite_id: str,
        *,
        status: Optional[str] = "active",
    ) -> List[TestCase]:
        """Fetch cases for a suite. Defaults to ``status='active'`` so
        suite-level / scheduled runs skip drafts and archived cases. Pass
        ``status=None`` to include every status (e.g. for UI listings).
        """
        stmt = select(TestCase).where(TestCase.suite_id == str(suite_id))
        if status is not None:
            stmt = stmt.where(TestCase.status == status)
        stmt = stmt.order_by(TestCase.created_at.asc())
        res = await db.execute(stmt)
        return res.scalars().all()

    async def _create_stub_report(self, db: AsyncSession, organization_id: str, user_id: str, title: str, ds_ids: Optional[List[str]] = None) -> Report:
        slug = f"testrun-{uuid.uuid4().hex[:12]}"
        report = Report(
            title=title,
            slug=slug,
            status="draft",
            report_type="test",
            user_id=user_id,
            organization_id=organization_id,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        # Associate data sources like regular reports
        try:
            if ds_ids:
                svc = ReportService()
                await svc._associate_data_sources_with_report(db, report, [str(x) for x in ds_ids if x])
                await db.refresh(report, ["data_sources"])
        except Exception:
            pass
        return report

    async def _create_head_completion(self, db: AsyncSession, report_id: str, organization, current_user, prompt: Dict[str, Any]) -> Completion:
        """
        Create a user head completion for a test result, aligned with CompletionService semantics:
        - Resolve model from prompt.model_id or organization default
        - Normalize prompt fields (e.g., widget_id as string or None)
        - Compute turn_index based on last completion in the report
        - Do not set 'completion' for user role
        """
        # Resolve model
        prompt_dict: Dict[str, Any] = dict(prompt or {})
        model_id = prompt_dict.get("model_id")
        model = None
        if model_id:
            try:
                model = await self.completions.llm_service.get_model_by_id(db, organization, current_user, model_id)
            except Exception:
                model = None
        if not model:
            model = await self.completions.llm_service.get_default_model_for_user(db, organization, current_user)
        if not model:
            raise HTTPException(status_code=400, detail="No default LLM model configured. Please configure a default model in organization settings.")

        # Normalize widget_id
        if prompt_dict.get("widget_id"):
            try:
                prompt_dict["widget_id"] = str(prompt_dict["widget_id"])
            except Exception:
                prompt_dict["widget_id"] = None
        else:
            prompt_dict["widget_id"] = None

        # Compute turn index
        last_completion = await self.completions.get_last_completion(db, report_id)
        turn_index = last_completion.turn_index + 1 if last_completion else 0

        head = Completion(
            prompt=prompt_dict or None,
            model=model.model_id,
            widget_id=prompt_dict.get("widget_id"),
            report_id=report_id,
            turn_index=turn_index,
            message_type="table",
            role="user",
            status="success",
            user_id=str(current_user.id) if current_user else None,
        )
        db.add(head)
        await db.commit()
        await db.refresh(head)

        # Best-effort: create mentions based on prompt content
        try:
            await self.completions.mention_service.create_completion_mentions(db, head)
        except Exception:
            pass

        return head

    async def create_run(self, db: AsyncSession, organization, current_user, case_ids: Optional[List[str]] = None, trigger_reason: Optional[str] = "manual", build_id: Optional[str] = None) -> TestRun:
        # Resolve cases set
        if not case_ids or len(case_ids) == 0:
            raise HTTPException(status_code=400, detail="case_ids is required")
        res = await db.execute(select(TestCase).where(TestCase.id.in_([str(c) for c in case_ids])))
        cases: List[TestCase] = res.scalars().all()
        if not cases:
            raise HTTPException(status_code=400, detail="No test cases found")
        # Ensure all cases belong to the same organization via their suites
        # (minimal guard: just ensure suites exist in org)
        suite_ids_set = set()
        for c in cases:
            _ = await self._get_suite(db, str(organization.id), str(c.suite_id))
            suite_ids_set.add(str(c.suite_id))

        # Build human-readable run title
        title: str
        case_names = [c.name for c in cases]
        preview = ", ".join(case_names[:2])
        remaining = max(0, len(case_names) - 2)
        title = preview + (f" +{remaining} more" if remaining > 0 else "")
        # If exactly one suite and all cases from that suite are included, you can later enhance
        # to compute "Suite Tests Run #N". For now, keep simple case-centric title.
        suite_ids_str = ",".join(sorted(suite_ids_set))

        # Resolve build_id: use provided or get current main build
        resolved_build_id = build_id
        if not resolved_build_id:
            # Get main build for this organization
            from app.models.instruction_build import InstructionBuild
            main_build_result = await db.execute(
                select(InstructionBuild).where(
                    InstructionBuild.organization_id == str(organization.id),
                    InstructionBuild.is_main == True,
                    InstructionBuild.deleted_at.is_(None)
                )
            )
            main_build = main_build_result.scalar_one_or_none()
            if main_build:
                resolved_build_id = str(main_build.id)

        # Create run
        run = TestRun(
            suite_ids=suite_ids_str,
            requested_by_user_id=str(current_user.id) if current_user else None,
            trigger_reason=trigger_reason or "manual",
            status="in_progress",
            started_at=datetime.utcnow(),
            title=title,
            build_id=resolved_build_id,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # Create placeholder TestResult per case (with stub report + head completion)
        for case in cases:
            report_title = f"Test Run · {case.name}"
            report = await self._create_stub_report(db, str(organization.id), str(current_user.id), report_title, ds_ids=case.data_source_ids_json)
            head = await self._create_head_completion(db, str(report.id), organization, current_user, prompt=case.prompt_json or {})

            # Build initial result_json snapshot from case expectations
            try:
                spec = dict(case.expectations_json or {})
                rules = spec.get("rules") or []
                result_json = {
                    "spec": {
                        "spec_version": spec.get("spec_version") or 1,
                        "rules": rules,
                        "order_mode": spec.get("order_mode"),
                    },
                    "totals": {
                        "total": len(rules),
                        "passed": 0,
                        "failed": 0,
                        "duration_ms": None,
                    },
                    "rule_results": [],
                }
            except Exception:
                result_json = None

            result = TestResult(
                run_id=str(run.id),
                case_id=str(case.id),
                head_completion_id=str(head.id),
                status="init",
                report_id=str(report.id),
                result_json=result_json,
            )
            db.add(result)
        await db.commit()

        return run

    async def run_suite(self, db: AsyncSession, organization, current_user, suite_id: str, background: bool = True) -> TestRun:
        # Get all cases for a suite and create a run
        cases = await self._get_cases(db, suite_id)
        if not cases:
            raise HTTPException(status_code=400, detail="No test cases found for this suite")
        return await self.create_run(db, organization, current_user, case_ids=[str(c.id) for c in cases], trigger_reason="manual")

        # The below implementation will be re-enabled once report linkage is added

    async def get_run(self, db: AsyncSession, organization_id: str, current_user, run_id: str) -> TestRun:
        res = await db.execute(select(TestRun).where(TestRun.id == run_id))
        run = res.scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Test run not found")
        # Ensure run's suites belong to org (best-effort check)
        for sid in (run.suite_ids.split(",") if getattr(run, "suite_ids", "") else []):
            _ = await self._get_suite(db, organization_id, str(sid))
        return run

    async def list_runs(self, db: AsyncSession, organization_id: str, current_user, suite_id: Optional[str] = None, status: Optional[str] = None, page: int = 1, limit: int = 20) -> List[TestRun]:
        stmt = select(TestRun)
        if status:
            stmt = stmt.where(TestRun.status == status)
        if suite_id:
            # Filter runs that include this suite by joining through results → cases
            await self._get_suite(db, organization_id, suite_id)
            from sqlalchemy.orm import aliased
            tr = TestRun
            tsr = TestResult
            tc = TestCase
            stmt = (
                select(tr).join(tsr, tsr.run_id == tr.id).join(tc, tc.id == tsr.case_id)
                .where(tc.suite_id == str(suite_id))
                .order_by(tr.created_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
                .distinct()
            )
            res = await db.execute(stmt)
            return res.scalars().all()
        stmt = stmt.order_by(TestRun.created_at.desc()).offset((page - 1) * limit).limit(limit)
        res = await db.execute(stmt)
        return res.scalars().all()

    async def list_results(self, db: AsyncSession, organization_id: str, current_user, run_id: str) -> List[TestResult]:
        _ = await self.get_run(db, organization_id, current_user, run_id)
        res = await db.execute(select(TestResult).where(TestResult.run_id == str(run_id)).order_by(TestResult.created_at.asc()))
        rows = res.scalars().all()
        # Normalize empty dicts to None to satisfy response schema when result_json is not populated yet
        for r in rows:
            try:
                if isinstance(getattr(r, "result_json", None), dict) and not getattr(r, "result_json"):
                    r.result_json = None
            except Exception:
                pass
        return rows

    async def get_result_transcript(
        self,
        db: AsyncSession,
        organization,
        current_user,
        result_id: str,
        max_messages: int = 40,
    ) -> str:
        """Render the message-context view of a TestResult's report using
        the same ``MessageContextBuilder`` the agent uses internally.

        This gives eval reports the same digests (tool summaries, row
        counts, viz ids, etc.) without duplicating the tool-specific
        logic here.
        """
        # Resolve result → report (org-scoped).
        res = await db.execute(select(TestResult).where(TestResult.id == str(result_id)))
        result = res.scalar_one_or_none()
        if not result:
            raise HTTPException(status_code=404, detail="Test result not found")
        _ = await self.get_run(db, str(organization.id), current_user, str(result.run_id))

        report = await db.get(Report, str(result.report_id))
        if not report:
            raise HTTPException(status_code=404, detail="Report for result not found")

        from app.ai.context.builders.message_context_builder import MessageContextBuilder
        # Ensure org.settings is loaded so the builder can read
        # allow_llm_see_data without triggering a lazy lookup from a
        # closed session.
        try:
            await organization.get_settings(db)
        except Exception:
            pass
        builder = MessageContextBuilder(db=db, organization=organization, report=report, user=current_user)
        return await builder.build_context(max_messages=max_messages)

    async def get_result(self, db: AsyncSession, organization_id: str, current_user, result_id: str) -> TestResult:
        res = await db.execute(select(TestResult).where(TestResult.id == result_id))
        result = res.scalar_one_or_none()
        if not result:
            raise HTTPException(status_code=404, detail="Test result not found")
        # ensure run -> suite in org
        _ = await self.get_run(db, organization_id, current_user, str(result.run_id))
        # Normalize empty dict to None for response validation
        try:
            if isinstance(getattr(result, "result_json", None), dict) and not getattr(result, "result_json"):
                result.result_json = None
        except Exception:
            pass
        return result

    async def stop_run(self, db: AsyncSession, organization_id: str, current_user, run_id: str) -> TestRun:
        """
        Gracefully stop an in-progress run:
        - Mark run.status = 'stopped' and set finished_at
        - Mark any in-progress results as 'error' with a failure_reason
        """
        res = await db.execute(select(TestRun).where(TestRun.id == run_id))
        run = res.scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Test run not found")
        # Ensure run's suites belong to org (best-effort guard across all suite_ids)
        for sid in (run.suite_ids.split(",") if getattr(run, "suite_ids", "") else []):
            _ = await self._get_suite(db, organization_id, str(sid))

        if getattr(run, "status", None) != "in_progress":
            return run
        
        # Fetch organization for audit logging
        organization = None
        try:
            org_result = await db.execute(
                select(Organization).where(Organization.id == organization_id)
            )
            organization = org_result.scalar_one_or_none()
        except Exception:
            pass
        
        # Send sigkill to any in-progress system completions for this run
        try:
            res_results = await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)))
            results_for_sigkill = res_results.scalars().all()
            for r in results_for_sigkill:
                try:
                    # Find system completions under the head for this result's report
                    sys_q = (
                        select(Completion)
                        .where(
                            Completion.report_id == str(r.report_id),
                            Completion.parent_id == str(r.head_completion_id),
                            Completion.role == "system",
                            Completion.status == "in_progress",
                        )
                        .order_by(Completion.created_at.desc())
                    )
                    res_sys = await db.execute(sys_q)
                    sys_completions = res_sys.scalars().all()
                    # Send sigkill using the same behavior as /api/completions/{id}/sigkill
                    # This ensures status is set to 'stopped', in-progress blocks are stopped,
                    # and websocket 'update_completion' is emitted for AgentV2 to cancel promptly.
                    for sc in sys_completions:
                        try:
                            await self.completions.update_completion_sigkill(db, str(sc.id), current_user, organization)
                        except Exception:
                            pass
                except Exception:
                    pass
            # update_completion_sigkill commits internally; no-op commit here
        except Exception:
            # Best-effort; proceed to stop run regardless
            pass
        
        # Mark run as stopped
        run.status = "stopped"
        run.finished_at = datetime.utcnow()
        db.add(run)
        await db.commit()

        # Mark any in-progress results as error for clarity
        res_results = await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)))
        results = res_results.scalars().all()
        changed = False
        for r in results:
            if getattr(r, "status", "") == "in_progress":
                r.status = "error"
                r.failure_reason = "Stopped by user"
                db.add(r)
                changed = True
        if changed:
            await db.commit()
        
        # Save run summary
        await self._save_run_summary(db, run, list(results))

        await db.refresh(run)

        # A stopped run is terminal — notify the origin conversation (if any).
        asyncio.create_task(self._maybe_fire_wake(str(run.id)))
        return run

    # ---- Dashboard helpers (mock data for MVP) ----
    async def get_dashboard_metrics(self, db: AsyncSession, organization_id: str, current_user) -> TestMetricsSchema:
        """
        Return accurate, org-scoped metrics for the dashboard:
        - total_test_cases: count of TestCase in org
        - total_test_runs: distinct count of TestRun in org (via results -> cases -> suites)
        - last_result_status/last_result_at: latest TestResult in org
        """
        # Total test cases in organization
        total_cases_stmt = (
            select(func.count(TestCase.id))
            .select_from(TestCase)
            .join(TestSuite, TestCase.suite_id == TestSuite.id)
            .where(TestSuite.organization_id == str(organization_id))
        )
        total_cases = (await db.execute(total_cases_stmt)).scalar_one() or 0

        # Total distinct runs that belong to organization (via results -> case -> suite filter)
        total_runs_stmt = (
            select(func.count(func.distinct(TestRun.id)))
            .select_from(TestRun)
            .join(TestResult, TestResult.run_id == TestRun.id)
            .join(TestCase, TestCase.id == TestResult.case_id)
            .join(TestSuite, TestSuite.id == TestCase.suite_id)
            .where(TestSuite.organization_id == str(organization_id))
        )
        total_runs = (await db.execute(total_runs_stmt)).scalar_one() or 0

        # Latest test result for the organization
        latest_result_stmt = (
            select(TestResult)
            .join(TestCase, TestCase.id == TestResult.case_id)
            .join(TestSuite, TestSuite.id == TestCase.suite_id)
            .where(TestSuite.organization_id == str(organization_id))
            .order_by(TestResult.created_at.desc())
            .limit(1)
        )
        latest_result = (await db.execute(latest_result_stmt)).scalar_one_or_none()
        last_status = getattr(latest_result, "status", None) if latest_result else None
        last_at = getattr(latest_result, "created_at", None) if latest_result else None

        return TestMetricsSchema(
            total_test_cases=int(total_cases),
            total_test_runs=int(total_runs),
            last_result_status=last_status,
            last_result_at=last_at,
        )

    async def get_suites_summary(self, db: AsyncSession, organization_id: str, current_user) -> List[TestSuiteSummarySchema]:
        # Return suites with counts and last run info
        res = await db.execute(select(TestSuite).where(TestSuite.organization_id == str(organization_id)).order_by(TestSuite.created_at.desc()))
        suites = res.scalars().all()
        summaries: List[TestSuiteSummarySchema] = []
        for s in suites:
            # tests_count = number of cases in suite
            res_cases = await db.execute(select(TestCase).where(TestCase.suite_id == str(s.id)))
            cases = res_cases.scalars().all()
            tests_count = len(cases)
            # last run (by picking latest TestRun that includes this suite via results → cases)
            res_run = await db.execute(
                select(TestRun)
                .join(TestResult, TestResult.run_id == TestRun.id)
                .join(TestCase, TestCase.id == TestResult.case_id)
                .where(TestCase.suite_id == str(s.id))
                .order_by(TestRun.created_at.desc())
                .limit(1)
            )
            run = res_run.scalar_one_or_none()
            last_run_at = getattr(run, 'created_at', None)
            last_status = getattr(run, 'status', None) if run else None
            # pass_rate not computed here (requires aggregating latest run results); leave None
            pass_rate = None
            summaries.append(TestSuiteSummarySchema(
                id=str(s.id),
                name=s.name,
                tests_count=tests_count,
                last_run_at=last_run_at,
                last_status=last_status,
                pass_rate=pass_rate,
            ))
        return summaries

    # -------- New API: Batch create + execute (background) --------
    async def create_and_execute_background(
        self,
        db: AsyncSession,
        organization,
        current_user,
        case_ids: Optional[List[str]] = None,
        suite_id: Optional[str] = None,
        trigger_reason: Optional[str] = "manual",
        build_id: Optional[str] = None,
        origin_report_id: Optional[str] = None,
        origin_user_id: Optional[str] = None,
        wake_on_finish: bool = False,
    ) -> tuple[TestRun, List[TestResult]]:
        # Resolve cases from inputs
        cases = await self._resolve_cases_inputs(db, str(organization.id), case_ids, suite_id)
        if not cases:
            raise HTTPException(status_code=400, detail="No test cases found")

        requested_case_ids = {str(c.id) for c in cases}
        in_progress_runs = await self._org_in_progress_runs(db, str(organization.id))

        # Resolve build_id up front (provided, else current main) so dedupe
        # compares what would actually be stored on the run.
        resolved_build_id = build_id
        if not resolved_build_id:
            from app.models.instruction_build import InstructionBuild
            main_build_result = await db.execute(
                select(InstructionBuild).where(
                    InstructionBuild.organization_id == str(organization.id),
                    InstructionBuild.is_main == True,
                    InstructionBuild.deleted_at.is_(None)
                )
            )
            main_build = main_build_result.scalar_one_or_none()
            if main_build:
                resolved_build_id = str(main_build.id)

        # Dedupe: an identical run (same build, same case set) already executing
        # is returned instead of duplicated — this also absorbs tool retries.
        for existing in in_progress_runs:
            if str(existing.build_id or "") != str(resolved_build_id or ""):
                continue
            existing_case_ids = set(
                (await db.execute(
                    select(TestResult.case_id).where(TestResult.run_id == str(existing.id))
                )).scalars().all()
            )
            if {str(x) for x in existing_case_ids} == requested_case_ids:
                # Adopt the wake request so the asking conversation still gets
                # notified when the already-running run finishes.
                if wake_on_finish and origin_report_id and not getattr(existing, "wake_on_finish", False):
                    existing.origin_report_id = str(origin_report_id)
                    existing.origin_user_id = str(origin_user_id) if origin_user_id else None
                    existing.wake_on_finish = True
                    db.add(existing)
                    await db.commit()
                    await db.refresh(existing)
                existing_results = (
                    await db.execute(
                        select(TestResult).where(TestResult.run_id == str(existing.id)).order_by(TestResult.created_at.asc())
                    )
                ).scalars().all()
                existing.deduped = True  # transient, serialized by TestRunSchema
                return existing, list(existing_results)

        # Org-level concurrency cap. Runs older than the watch timeout are
        # excluded — a pre-existing stuck run (e.g. from before the background
        # finalizer existed) must not consume the budget forever.
        max_concurrent = DEFAULT_MAX_CONCURRENT_EVAL_RUNS
        try:
            settings = await organization.get_settings(db)
            configured = settings.get_config("max_concurrent_eval_runs", DEFAULT_MAX_CONCURRENT_EVAL_RUNS)
            if isinstance(configured, (int, float)) and int(configured) > 0:
                max_concurrent = int(configured)
        except Exception:
            pass
        from datetime import timedelta
        stale_cutoff = datetime.utcnow() - timedelta(seconds=CASE_WATCH_TIMEOUT_S + 60)
        countable = [
            r for r in in_progress_runs
            if (getattr(r, "started_at", None) or datetime.utcnow()) > stale_cutoff
        ]
        if len(countable) >= max_concurrent:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"{len(countable)} eval runs are already in progress "
                    f"(org limit {max_concurrent}). Wait for one to finish or stop it."
                ),
            )

        # Create run
        case_names = [c.name for c in cases]
        preview = ", ".join(case_names[:2])
        remaining = max(0, len(case_names) - 2)
        title = preview + (f" +{remaining} more" if remaining > 0 else "")
        suite_ids_set = {str(c.suite_id) for c in cases}
        suite_ids_str = ",".join(sorted(suite_ids_set))

        run = TestRun(
            suite_ids=suite_ids_str,
            requested_by_user_id=str(current_user.id) if current_user else None,
            trigger_reason=trigger_reason or "manual",
            status="in_progress",
            started_at=datetime.utcnow(),
            title=title,
            build_id=resolved_build_id,
            origin_report_id=str(origin_report_id) if origin_report_id else None,
            origin_user_id=str(origin_user_id) if origin_user_id else None,
            wake_on_finish=bool(wake_on_finish and origin_report_id),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        created_results: List[TestResult] = []
        watches: List[Dict[str, Any]] = []

        # For each case: create a report and kick off background completion via CompletionService
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        for case in cases:
            report_title = f"Test Run · {case.name}"
            report = await self._create_stub_report(db, str(organization.id), str(current_user.id), report_title, ds_ids=getattr(case, "data_source_ids_json", None))

            # Build prompt schema for turn 1
            p = case.prompt_json or {}
            prompt = PromptSchema(
                content=p.get("content") or "",
                widget_id=None,
                step_id=None,
                mentions=p.get("mentions"),
                mode=p.get("mode"),
                model_id=p.get("model_id"),
            )
            completion_data = CompletionCreate(prompt=prompt)

            additional_turns = list(getattr(case, "additional_turns_json", None) or [])

            # Create head+system and run agent in background using existing service
            # Pass resolved_build_id so agent uses correct instruction build
            v2 = await self.completions.create_completion(
                db=db,
                report_id=str(report.id),
                completion_data=completion_data,
                current_user=current_user,
                organization=organization,
                background=True,
                build_id=resolved_build_id,
            )

            # Extract head completion id (user role) from the returned list
            head_id = None
            try:
                for c in (v2.completions or []):
                    if getattr(c, "role", None) == "user" and c.parent_id is None and str(getattr(c, "report_id", "")) == str(report.id):
                        head_id = str(c.id)
                        break
            except Exception:
                head_id = None

            # Multi-turn: thread remaining turns after turn 1's agent finishes.
            if additional_turns and head_id:
                asyncio.create_task(
                    self._run_additional_turns_background(
                        report_id=str(report.id),
                        head_completion_id=str(head_id),
                        additional_turns=additional_turns,
                        organization=organization,
                        current_user=current_user,
                        build_id=resolved_build_id,
                    )
                )

            result = TestResult(
                run_id=str(run.id),
                case_id=str(case.id),
                head_completion_id=str(head_id) if head_id else str(uuid.uuid4()),  # fallback placeholder
                status="in_progress",
                report_id=str(report.id),
                result_json={
                    "spec": {
                        "spec_version": (case.expectations_json or {}).get("spec_version") or 1,
                        "rules": (case.expectations_json or {}).get("rules") or [],
                        "order_mode": (case.expectations_json or {}).get("order_mode"),
                    },
                    "totals": {
                        "total": len(((case.expectations_json or {}).get("rules") or [])),
                        "passed": 0,
                        "failed": 0,
                        "duration_ms": None,
                    },
                    "rule_results": [],
                },
            )
            db.add(result)
            created_results.append(result)
            watches.append({
                "report_id": str(report.id),
                "expected_turns": 1 + len(additional_turns),
            })

        await db.commit()
        # refresh results to include IDs
        for r in created_results:
            await db.refresh(r)
        for w, r in zip(watches, created_results):
            w["result_id"] = str(r.id)

        # Arm the background finalizer: waits for each case's agent turn(s),
        # evaluates expectations, finalizes the run, fires wake-on-finish.
        asyncio.create_task(
            self._watch_and_finalize(
                run_id=str(run.id),
                organization_id=str(organization.id),
                user_id=str(current_user.id),
                watches=watches,
            )
        )

        return run, created_results

    async def _run_additional_turns_background(
        self,
        *,
        report_id: str,
        head_completion_id: str,
        additional_turns: List[Dict[str, Any]],
        organization,
        current_user,
        build_id: Optional[str],
    ) -> None:
        """Run turns 2..N sequentially on the same report once turn 1 finishes.

        Each turn is dispatched via ``CompletionService.create_completion`` with
        ``background=False`` so we block until the agent terminates before
        dispatching the next turn. Opens its own async session because the
        request-scoped session is already closed by the time this runs.
        """
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema

        async_session = create_async_session_factory()
        try:
            # Wait for turn 1's agent to leave in_progress.
            await self._await_head_completion_done(async_session, head_completion_id)

            for turn in additional_turns:
                p = (turn or {}).get("prompt") or {}
                prompt = PromptSchema(
                    content=p.get("content") or "",
                    widget_id=None,
                    step_id=None,
                    mentions=p.get("mentions"),
                    mode=p.get("mode"),
                    model_id=p.get("model_id"),
                )
                async with async_session() as session:
                    # Synchronous path: blocks until this turn's agent is done.
                    await self.completions.create_completion(
                        db=session,
                        report_id=str(report_id),
                        completion_data=CompletionCreate(prompt=prompt),
                        current_user=current_user,
                        organization=organization,
                        background=False,
                        build_id=build_id,
                    )
        except Exception:
            # Best-effort; evaluator will observe whatever state the report
            # reached and mark the TestResult accordingly on the next status
            # poll / stream.
            pass

    async def _await_head_completion_done(
        self,
        async_session,
        head_completion_id: str,
        *,
        poll_interval_s: float = 1.0,
        timeout_s: float = 600.0,
    ) -> None:
        """Poll the system completion for a head until it reaches a terminal state."""
        import time
        terminal = {"success", "error", "stopped"}
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            async with async_session() as session:
                res = await session.execute(
                    select(Completion)
                    .where(
                        Completion.parent_id == str(head_completion_id),
                        Completion.role == "system",
                    )
                    .order_by(Completion.created_at.desc())
                    .limit(1)
                )
                sys_comp = res.scalar_one_or_none()
                if sys_comp is not None and getattr(sys_comp, "status", None) in terminal:
                    return
            await asyncio.sleep(poll_interval_s)

    # -------- Background finalizer (evaluate + finalize without a streaming client) --------

    async def _finalize_run_if_done(self, session: AsyncSession, run_id: str) -> Optional[TestRun]:
        """Stamp the run terminal (status/finished_at/summary_json) once every
        TestResult is terminal. Returns the run when it *just* transitioned,
        None otherwise. Safe to call from multiple watchers/streamers — the
        first caller to see all-terminal wins; later calls no-op."""
        run = (
            await session.execute(
                select(TestRun).where(TestRun.id == str(run_id)).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if not run or run.status in RUN_TERMINAL_STATUSES:
            return None
        rows = (
            await session.execute(
                select(TestResult).where(TestResult.run_id == str(run_id)).execution_options(populate_existing=True)
            )
        ).scalars().all()
        statuses = [getattr(r, "status", "") for r in rows]
        if not rows or any(s not in RESULT_TERMINAL_STATUSES for s in statuses):
            return None
        run.finished_at = run.finished_at or datetime.utcnow()
        if any(s == "stopped" for s in statuses):
            run.status = "stopped"
        else:
            run.status = "success" if all(s not in {"fail", "error"} for s in statuses) else "error"
        passed = sum(1 for s in statuses if s == "pass")
        failed = sum(1 for s in statuses if s in ("fail", "error"))
        run.summary_json = {"total": len(rows), "passed": passed, "failed": failed}
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run

    async def _maybe_fire_wake(self, run_id: str) -> None:
        """Fire the wake-on-finish completion for a terminal run, at most once.

        The flag is flipped off *before* the completion is created so a racing
        second caller no-ops. Best-effort: a lost wake degrades to the agent
        polling with get_eval_run (or a user checking the UI) — it must never
        block or fail run finalization.
        """
        async_session = create_async_session_factory()
        try:
            async with async_session() as session:
                run = (
                    await session.execute(select(TestRun).where(TestRun.id == str(run_id)))
                ).scalar_one_or_none()
                if (
                    not run
                    or not getattr(run, "wake_on_finish", False)
                    or not getattr(run, "origin_report_id", None)
                    or run.status not in RUN_TERMINAL_STATUSES
                ):
                    return
                # Claim the wake before doing anything visible.
                run.wake_on_finish = False
                origin_report_id = str(run.origin_report_id)
                origin_user_id = str(run.origin_user_id) if run.origin_user_id else None
                summary = dict(run.summary_json or {})
                run_status = run.status
                run_title = run.title
                session.add(run)
                await session.commit()

            from app.models.user import User
            from app.models.organization import Organization as _Org

            async with async_session() as session:
                report = await session.get(Report, origin_report_id)
                if not report or getattr(report, "deleted_at", None):
                    logging.warning(f"eval wake {run_id}: origin report gone")
                    return
                user = await session.get(User, origin_user_id) if origin_user_id else None
                organization = await session.get(_Org, str(report.organization_id))
                if not user or not organization:
                    logging.warning(f"eval wake {run_id}: origin user/org gone")
                    return

                passed = summary.get("passed", 0)
                total = summary.get("total", 0)
                wake_prompt = (
                    f"[Eval run finished] Run '{run_title}' ({run_id}) completed with "
                    f"status {run_status} ({passed}/{total} passed). Read the results "
                    f"with get_eval_run and report back to the user. If this run was "
                    f"already handled in the conversation, acknowledge briefly and stop."
                )
                # Machine turn: a visible event strip + hidden trigger, not a
                # fake user bubble. mode='training' — run_eval is training-only,
                # so the origin conversation is a training session; without it
                # the woken turn runs in chat mode where get_eval_run isn't in
                # the catalog and the agent can't actually read the results.
                from app.services.machine_turn import run_machine_turn

                await run_machine_turn(
                    session,
                    report=report,
                    user=user,
                    organization=organization,
                    summary=(
                        f"Eval run '{run_title}' finished — "
                        f"{passed}/{total} passed ({run_status})"
                    ),
                    trigger_source="eval_run",
                    message_type="eval_run_event",
                    instruction=wake_prompt,
                    mode="training",
                )
        except Exception as e:
            logging.error(f"eval wake for run {run_id} failed: {e}")

    async def _evaluate_report_result(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        report_id: str,
        organization,
        current_user,
        org_settings,
        small_model,
    ) -> Optional[str]:
        """Evaluate a finished case report and persist its TestResult.

        Mirrors the terminal-completion branch of ``stream_run``. Returns the
        persisted status, or None when the result was already terminal
        (evaluated by a concurrent streamer)."""
        _run, result_row, case_row, expectations = await self.evaluator.resolve_by_run_and_report(
            session, str(run_id), str(report_id)
        )
        if getattr(result_row, "status", "") in RESULT_TERMINAL_STATUSES:
            return None
        snapshot = await self.evaluator.build_final_snapshot(session, str(report_id))
        try:
            judge = Judge(model=small_model, organization_settings=org_settings) if small_model else None
        except Exception:
            judge = None

        # Latest system completion for this report → AgentExecution metadata
        agent_execution_id = None
        run_duration_ms = None
        agent_meta: Dict[str, Any] = {}
        try:
            sys_row = (
                await session.execute(
                    select(Completion)
                    .where(
                        Completion.report_id == str(report_id),
                        Completion.role == "system",
                    )
                    .order_by(Completion.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if sys_row is not None:
                res_exec = await session.execute(
                    select(AgentExecution)
                    .where(AgentExecution.completion_id == str(sys_row.id))
                    .order_by(AgentExecution.created_at.desc())
                    .limit(1)
                )
                ae = res_exec.scalar_one_or_none()
                if ae:
                    agent_execution_id = str(ae.id)
                    run_duration_ms = getattr(ae, "total_duration_ms", None)
                    agent_meta = _agent_metadata_from_execution(ae)
                    try:
                        from app.models.plan_decision import PlanDecision as _PD
                        n_iter = (await session.execute(
                            select(func.count(_PD.id)).where(_PD.agent_execution_id == str(ae.id))
                        )).scalar_one() or 0
                        agent_meta["total_iterations"] = int(n_iter)
                    except Exception:
                        pass
        except Exception:
            pass

        case_prompt_text = ""
        try:
            cj = getattr(case_row, "prompt_json", None) or {}
            case_prompt_text = cj.get("content") or ""
        except Exception:
            case_prompt_text = ""

        status, result_json = await self.evaluator.evaluate_final(
            db=session,
            expectations=expectations,
            snapshot=snapshot,
            report_id=str(report_id),
            case_prompt_text=case_prompt_text,
            judge=judge,
            organization=organization,
            current_user=current_user,
            run_duration_ms=run_duration_ms,
            agent_metadata=agent_meta,
        )
        await self.evaluator.persist_result_json(
            db=session,
            result=result_row,
            status=status,
            result_json=result_json,
            failure_reason=None,
            agent_execution_id=agent_execution_id,
        )
        return status

    async def _watch_case(
        self,
        async_session,
        *,
        run_id: str,
        result_id: str,
        report_id: str,
        expected_turns: int,
        organization_id: str,
        user_id: str,
        timeout_s: float = CASE_WATCH_TIMEOUT_S,
    ) -> None:
        """Wait for a case's agent turn(s) to finish, then evaluate + persist."""
        import time as _time

        deadline = _time.monotonic() + timeout_s
        timed_out = True
        while _time.monotonic() < deadline:
            async with async_session() as session:
                result_row = await session.get(TestResult, str(result_id))
                if result_row is not None and getattr(result_row, "status", "") in RESULT_TERMINAL_STATUSES:
                    return  # evaluated elsewhere (e.g. a live stream_run client)
                sys_rows = (
                    await session.execute(
                        select(Completion.status).where(
                            Completion.report_id == str(report_id),
                            Completion.role == "system",
                        )
                    )
                ).scalars().all()
                terminal_n = sum(1 for s in sys_rows if s in {"success", "error", "stopped"})
                any_in_progress = any(s == "in_progress" for s in sys_rows)
                if terminal_n >= max(1, int(expected_turns)) and not any_in_progress:
                    timed_out = False
                    break
            await asyncio.sleep(2.0)

        from app.models.user import User
        from app.models.organization import Organization as _Org

        async with async_session() as session:
            try:
                if timed_out:
                    result_row = await session.get(TestResult, str(result_id))
                    if result_row is not None and getattr(result_row, "status", "") not in RESULT_TERMINAL_STATUSES:
                        result_row.status = "error"
                        result_row.failure_reason = f"Timed out after {int(timeout_s)}s waiting for the agent to finish"
                        session.add(result_row)
                        await session.commit()
                    return

                organization = await session.get(_Org, str(organization_id))
                current_user = await session.get(User, str(user_id))
                org_settings = await organization.get_settings(session) if organization else None
                small_model = None
                try:
                    small_model = await self.completions.llm_service.get_default_model(
                        session, organization, current_user, is_small=True
                    )
                except Exception:
                    small_model = None
                await self._evaluate_report_result(
                    session,
                    run_id=str(run_id),
                    report_id=str(report_id),
                    organization=organization,
                    current_user=current_user,
                    org_settings=org_settings,
                    small_model=small_model,
                )
            except Exception as e:
                logging.exception(f"eval watcher: case evaluation failed for result {result_id}: {e}")
                try:
                    result_row = await session.get(TestResult, str(result_id))
                    if result_row is not None and getattr(result_row, "status", "") not in RESULT_TERMINAL_STATUSES:
                        result_row.status = "error"
                        result_row.failure_reason = f"Evaluation failed: {e}"
                        session.add(result_row)
                        await session.commit()
                except Exception:
                    pass

    async def _watch_and_finalize(
        self,
        *,
        run_id: str,
        organization_id: str,
        user_id: str,
        watches: List[Dict[str, Any]],
    ) -> None:
        """Background task armed by ``create_and_execute_background``: drive
        every case to a terminal TestResult, finalize the run, fire the wake.

        This is what makes background runs self-contained — before this,
        evaluation only ever happened inside ``stream_run``, so a run nobody
        streamed stayed 'in_progress' forever."""
        async_session = create_async_session_factory()
        try:
            await asyncio.gather(
                *[
                    self._watch_case(
                        async_session,
                        run_id=str(run_id),
                        result_id=str(w["result_id"]),
                        report_id=str(w["report_id"]),
                        expected_turns=int(w.get("expected_turns") or 1),
                        organization_id=str(organization_id),
                        user_id=str(user_id),
                    )
                    for w in watches
                ]
            )
        except Exception as e:
            logging.exception(f"eval watcher: run {run_id} watch failed: {e}")
        try:
            async with async_session() as session:
                finalized = await self._finalize_run_if_done(session, str(run_id))
            if finalized is not None:
                await self._maybe_fire_wake(str(run_id))
        except Exception as e:
            logging.exception(f"eval watcher: run {run_id} finalize failed: {e}")

    async def _org_in_progress_runs(self, db: AsyncSession, organization_id: str) -> List[TestRun]:
        """All in-progress runs belonging to this org (scoped via the suite chain)."""
        rows = (
            await db.execute(
                select(TestRun).where(TestRun.status == "in_progress").execution_options(populate_existing=True)
            )
        ).scalars().all()
        out: List[TestRun] = []
        for run in rows:
            sids = [s for s in (run.suite_ids or "").split(",") if s]
            if not sids:
                continue
            org_row = (
                await db.execute(
                    select(TestSuite.id)
                    .where(TestSuite.id == sids[0])
                    .where(TestSuite.organization_id == str(organization_id))
                )
            ).first()
            if org_row is not None:
                out.append(run)
        return out

    # -------- Run comparison (build-over-build) --------

    async def compare_runs(
        self,
        db: AsyncSession,
        organization_id: str,
        current_user,
        run_id: str,
        against_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare a run's per-case outcomes against a baseline run.

        Default baseline: the most recent terminal run started before this one
        that shares at least one case. Flip semantics (relative to baseline →
        this run): fixed = fail/error→pass, regressed = pass→fail/error,
        added/removed = case present in only one of the runs.
        """
        run = await self.get_run(db, organization_id, current_user, run_id)
        run_rows = (
            await db.execute(
                select(TestResult, TestCase.name)
                .join(TestCase, TestCase.id == TestResult.case_id)
                .where(TestResult.run_id == str(run.id))
            )
        ).all()
        run_status_by_case = {str(r.case_id): (r.status or "", name) for r, name in run_rows}

        base_run: Optional[TestRun] = None
        if against_run_id:
            base_run = await self.get_run(db, organization_id, current_user, against_run_id)
        else:
            candidates = (
                await db.execute(
                    select(TestRun)
                    .where(TestRun.id != str(run.id))
                    .where(TestRun.status.in_(list(RUN_TERMINAL_STATUSES)))
                    .where(TestRun.created_at <= run.created_at)
                    .order_by(TestRun.created_at.desc())
                    .limit(50)
                )
            ).scalars().all()
            for cand in candidates:
                # Org scope via suite chain (same guard get_run uses).
                sids = [s for s in (cand.suite_ids or "").split(",") if s]
                if not sids:
                    continue
                in_org = (
                    await db.execute(
                        select(TestSuite.id)
                        .where(TestSuite.id == sids[0])
                        .where(TestSuite.organization_id == str(organization_id))
                    )
                ).first()
                if in_org is None:
                    continue
                cand_case_ids = set(
                    (await db.execute(
                        select(TestResult.case_id).where(TestResult.run_id == str(cand.id))
                    )).scalars().all()
                )
                if {str(x) for x in cand_case_ids} & set(run_status_by_case.keys()):
                    base_run = cand
                    break

        def _run_brief(r: Optional[TestRun]) -> Optional[Dict[str, Any]]:
            if r is None:
                return None
            return {
                "id": str(r.id),
                "title": r.title,
                "status": r.status,
                "build_id": str(r.build_id) if r.build_id else None,
                "build_number": getattr(r, "build_number", None),
                "finished_at": r.finished_at.isoformat() if getattr(r, "finished_at", None) else None,
                "summary": r.summary_json or {},
            }

        if base_run is None:
            return {
                "run": _run_brief(run),
                "against_run": None,
                "cases": [],
                "summary": {"fixed": 0, "regressed": 0, "same": 0, "added": len(run_status_by_case), "removed": 0},
            }

        base_rows = (
            await db.execute(
                select(TestResult, TestCase.name)
                .join(TestCase, TestCase.id == TestResult.case_id)
                .where(TestResult.run_id == str(base_run.id))
            )
        ).all()
        base_status_by_case = {str(r.case_id): (r.status or "", name) for r, name in base_rows}

        failing = {"fail", "error"}
        cases: List[Dict[str, Any]] = []
        summary = {"fixed": 0, "regressed": 0, "same": 0, "added": 0, "removed": 0}
        for cid in sorted(set(run_status_by_case) | set(base_status_by_case)):
            cur = run_status_by_case.get(cid)
            base = base_status_by_case.get(cid)
            if cur and not base:
                flip = "added"
            elif base and not cur:
                flip = "removed"
            else:
                cur_s, base_s = cur[0], base[0]
                if base_s in failing and cur_s == "pass":
                    flip = "fixed"
                elif base_s == "pass" and cur_s in failing:
                    flip = "regressed"
                else:
                    flip = "same"
            summary[flip] += 1
            cases.append({
                "case_id": cid,
                "case_name": (cur or base)[1],
                "base_status": base[0] if base else None,
                "status": cur[0] if cur else None,
                "flip": flip,
            })

        return {
            "run": _run_brief(run),
            "against_run": _run_brief(base_run),
            "cases": cases,
            "summary": summary,
        }

    # -------- New API: Run status with embedded completions (polling) --------
    async def get_run_status_with_completions(self, db: AsyncSession, organization, current_user, run_id: str, limit: int = 50):
        # Load run and validate
        run = await self.get_run(db, str(organization.id), current_user, run_id)
        # Get all results
        res = await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)).order_by(TestResult.created_at.asc()))
        results = res.scalars().all()
        # Normalize empty result_json dicts to None for response validation
        for r in results:
            try:
                if isinstance(getattr(r, "result_json", None), dict) and not getattr(r, "result_json"):
                    r.result_json = None
            except Exception:
                pass

        # For each result, fetch completions v2 (limited) and unwrap to list
        from app.schemas.completion_v2_schema import CompletionV2Schema
        items: List[dict] = []
        for r in results:
            try:
                v2 = await self.completions.get_completions_v2(db, str(r.report_id), organization=organization, current_user=current_user, limit=limit)
            except Exception:
                v2 = None
            completions_list: List[CompletionV2Schema] = []
            if v2 and getattr(v2, "completions", None):
                completions_list = list(v2.completions)
            items.append({
                "result": r,
                "report_id": str(r.report_id),
                "completions": completions_list,
            })
        return run, items

    async def get_test_completions(self, db: AsyncSession, organization, current_user, run_id: str, limit: int = 10):
        """
        Lightweight helper that returns recent completions per result for a test run.
        Shape: [{ 'result_id': str, 'report_id': str, 'completions': [CompletionV2Schema, ...] }, ...]
        """
        run, items = await self.get_run_status_with_completions(db, organization, current_user, run_id, limit=limit)
        out: list[dict] = []
        for it in items:
            try:
                result = it.get("result")
                out.append({
                    "result_id": str(getattr(result, "id")),
                    "report_id": it.get("report_id"),
                    "completions": it.get("completions") or [],
                })
            except Exception:
                pass
        return out

    # -------- New API: Per-result streaming using existing CompletionService --------
    async def stream_result(self, db: AsyncSession, organization, current_user, result_id: str):
        # Get result, case, report
        result = await self.get_result(db, str(organization.id), current_user, result_id)
        res_case = await db.execute(select(TestCase).where(TestCase.id == str(result.case_id)))
        case = res_case.scalar_one_or_none()
        if not case:
            raise HTTPException(status_code=404, detail="Test case not found")

        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        p = case.prompt_json or {}
        prompt = PromptSchema(
            content=p.get("content") or "",
            widget_id=None,
            step_id=None,
            mentions=p.get("mentions"),
            mode=p.get("mode"),
            model_id=p.get("model_id"),
        )
        completion_data = CompletionCreate(prompt=prompt)
        # Get build_id from the run
        run = await self.get_run(db, str(organization.id), current_user, str(result.run_id))
        build_id = str(run.build_id) if run.build_id else None
        # Delegate to existing streaming method (creates head+system and streams AgentV2)
        return await self.completions.create_completion_stream(
            db=db,
            report_id=str(result.report_id),
            completion_data=completion_data,
            current_user=current_user,
            organization=organization,
            build_id=build_id,
        )

    # -------- New API: Run-level streaming (start all INIT results and stream status updates) --------
    async def stream_run(self, db: AsyncSession, organization, current_user, run_id: str):
        """
        Start execution for any INIT results in a run and stream high-level status updates.
        Note: logs/blocks are available via per-result completion APIs; this stream focuses on lifecycle.
        """
        from app.schemas.sse_schema import SSEEvent, format_sse_event
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        from fastapi.responses import StreamingResponse
        import asyncio

        # Validate run and fetch results
        run = await self.get_run(db, str(organization.id), current_user, run_id)
        res = await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)).order_by(TestResult.created_at.asc()))
        results = res.scalars().all()
        result_id_to_status: dict[str, str] = {str(r.id): getattr(r, "status", "") for r in results}

        async def start_pending_with_queues(central_queue: "asyncio.Queue[tuple[str, SSEEvent]]"):
            """
            For each INIT result, create a system completion and start AgentV2 with an event queue.
            Forward events into central_queue with the result_id.
            """
            async def forward_events(res_id: str, q: CompletionEventQueue):
                async for ev in q.get_events():
                    try:
                        # Wrap data with result_id to allow demux on client
                        if isinstance(ev.data, dict):
                            data = dict(ev.data)
                            data["result_id"] = res_id
                        else:
                            data = {"result_id": res_id, "payload": ev.data}
                        wrapped = SSEEvent(event=ev.event, completion_id=ev.completion_id, data=data)
                        await central_queue.put((res_id, wrapped))
                    except Exception:
                        pass

            org_settings = await organization.get_settings(db)

            for r in results:
                # Process results that are not terminal; include both 'init' and 'in_progress'
                if getattr(r, "status", "") in {"pass", "fail", "error", "stopped", "success"}:
                    continue

                # Load case + head completion
                res_case = await db.execute(select(TestCase).where(TestCase.id == str(r.case_id)))
                case = res_case.scalar_one_or_none()
                if not case:
                    continue
                head = await db.get(Completion, str(r.head_completion_id))
                if not head:
                    continue
                # Pre-store expectations spec into result_json so UI shows pending assertions immediately.
                # Only backfill when the snapshot is missing to avoid overwriting historical specs.
                try:
                    spec_raw = dict(getattr(r, "result_json", {}) or {}).get("spec")
                    existing_rules = []
                    if isinstance(spec_raw, dict):
                        existing_rules = spec_raw.get("rules") or []
                    if not existing_rules:
                        case_spec_raw = dict(getattr(case, "expectations_json", {}) or {})
                        rules = list(case_spec_raw.get("rules") or [])
                        current_rj = getattr(r, "result_json", None)
                        if not isinstance(current_rj, dict):
                            current_rj = {}
                        current_rj["spec"] = {
                            "spec_version": case_spec_raw.get("spec_version") or 1,
                            "rules": rules,
                            "order_mode": case_spec_raw.get("order_mode"),
                        }
                        existing_rules = rules
                        r.result_json = current_rj
                    # Ensure totals present and aligned with whichever spec we're using
                    current_rj = getattr(r, "result_json", None)
                    if isinstance(current_rj, dict):
                        totals = dict(current_rj.get("totals") or {})
                        totals.setdefault("passed", 0)
                        totals.setdefault("failed", 0)
                        totals.setdefault("duration_ms", None)
                        totals["total"] = len(existing_rules or [])
                        current_rj["totals"] = totals
                        if not isinstance(current_rj.get("rule_results"), list):
                            current_rj["rule_results"] = []
                        r.result_json = current_rj
                        db.add(r)
                        await db.commit()
                except Exception:
                    pass
                # Detect an existing system completion for this head/report (latest)
                existing_system = None
                try:
                    existing_system = (
                        await db.execute(
                            select(Completion)
                            .where(
                                Completion.report_id == str(head.report_id),
                                Completion.parent_id == str(head.id),
                                Completion.role == "system",
                            )
                            .order_by(Completion.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                except Exception:
                    existing_system = None

                p = case.prompt_json or {}
                prompt = PromptSchema(
                    content=p.get("content") or "",
                    widget_id=None,
                    step_id=None,
                    mentions=p.get("mentions"),
                    mode=p.get("mode"),
                    model_id=p.get("model_id"),
                )

                # Resolve models
                model = None
                if prompt.model_id:
                    try:
                        model = await self.completions.llm_service.get_model_by_id(db, organization, current_user, prompt.model_id)
                    except Exception:
                        model = None
                if not model:
                    model = await self.completions.llm_service.get_default_model_for_user(db, organization, current_user)
                small_model = await self.completions.llm_service.get_default_model(db, organization, current_user, is_small=True)
                if not model:
                    # Cannot start - mark error
                    try:
                        r.status = "error"
                        r.failure_reason = "No default LLM model"
                        db.add(r)
                        await db.commit()
                    except Exception:
                        pass
                    continue

                # If an existing terminal system completion exists, evaluate immediately and emit update
                if existing_system and getattr(existing_system, "status", "") in {"success", "error", "stopped"}:
                    try:
                        # Evaluate and persist
                        async_session = create_async_session_factory()
                        async with async_session() as session:
                            from app.models.report import Report as _Report
                            report_obj = await session.get(_Report, head.report_id)
                            _run, result_row, case_row, expectations = await self.evaluator.resolve_by_run_and_report(
                                session, str(run.id), str(head.report_id)
                            )
                            snapshot = await self.evaluator.build_final_snapshot(session, str(head.report_id))
                            judge = None
                            try:
                                judge = Judge(model=small_model, organization_settings=org_settings)
                            except Exception:
                                judge = None
                            # Determine AgentExecution and duration
                            agent_execution_id = None
                            run_duration_ms = None
                            agent_meta: Dict[str, Any] = {}
                            try:
                                res_exec = await session.execute(
                                    select(AgentExecution)
                                    .where(AgentExecution.completion_id == str(existing_system.id))
                                    .order_by(AgentExecution.created_at.desc())
                                    .limit(1)
                                )
                                ae = res_exec.scalar_one_or_none()
                                if ae:
                                    agent_execution_id = str(ae.id)
                                    run_duration_ms = getattr(ae, "total_duration_ms", None)
                                    agent_meta = _agent_metadata_from_execution(ae)
                                    try:
                                        from app.models.plan_decision import PlanDecision as _PD
                                        n_iter = (await session.execute(
                                            select(func.count(_PD.id)).where(_PD.agent_execution_id == str(ae.id))
                                        )).scalar_one() or 0
                                        agent_meta["total_iterations"] = int(n_iter)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            case_prompt_text = ""
                            try:
                                cj = getattr(case_row, "prompt_json", None) or {}
                                case_prompt_text = cj.get("content") or ""
                            except Exception:
                                case_prompt_text = ""
                            status, result_json = await self.evaluator.evaluate_final(
                                db=session,
                                expectations=expectations,
                                snapshot=snapshot,
                                report_id=str(head.report_id),
                                case_prompt_text=case_prompt_text,
                                judge=judge,
                                organization=organization,
                                current_user=current_user,
                                run_duration_ms=run_duration_ms,
                                agent_metadata=agent_meta,
                            )
                            await self.evaluator.persist_result_json(
                                db=session,
                                result=result_row,
                                status=status,
                                result_json=result_json,
                                failure_reason=None,
                                agent_execution_id=agent_execution_id,
                            )
                        # Emit UI update to central queue
                        try:
                            payload = {
                                "result_id": str(r.id),
                                "status": status,
                                "result_json": result_json.model_dump(),
                            }
                            await central_queue.put((str(r.id), SSEEvent(event="result.update", completion_id=str(existing_system.id), data=payload)))
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # Proceed to next result
                    continue

                # If an existing in-progress system completion exists, emit started and skip new run
                if existing_system and getattr(existing_system, "status", "") == "in_progress":
                    try:
                        start_ev = SSEEvent(
                            event="completion.started",
                            completion_id=str(existing_system.id),
                            data={"result_id": str(r.id), "system_completion_id": str(existing_system.id), "head_completion_id": str(head.id)},
                        )
                        await central_queue.put((str(r.id), start_ev))
                    except Exception:
                        pass
                    # Do not start a duplicate agent; rely on other runner to finish, streamer loop will pick result.update if persisted elsewhere
                    continue

                # Otherwise, create system completion linked to the existing head and start agent
                system_completion = Completion(
                    prompt=None,
                    completion={"content": ""},
                    model=model.model_id,
                    widget_id=head.widget_id,
                    report_id=head.report_id,
                    parent_id=head.id,
                    turn_index=head.turn_index + 1,
                    message_type="table",
                    role="system",
                    status="in_progress",
                )
                try:
                    db.add(system_completion)
                    await db.commit()
                    await db.refresh(system_completion)
                except Exception:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    continue

                # Mark in_progress
                r.status = "in_progress"
                db.add(r)
                await db.commit()

                # Event queue per result
                eq = CompletionEventQueue()

                # Emit completion.started (with system id) to central queue
                try:
                    start_ev = SSEEvent(
                        event="completion.started",
                        completion_id=str(system_completion.id),
                        data={"result_id": str(r.id), "system_completion_id": str(system_completion.id), "head_completion_id": str(head.id)},
                    )
                    await central_queue.put((str(r.id), start_ev))
                except Exception:
                    pass

                async def run_agent_task():
                    async_session = create_async_session_factory()
                    async with async_session() as session:
                        try:
                            report_obj = await session.get(Report, head.report_id)
                            head_obj = await session.get(Completion, head.id)
                            system_obj = await session.get(Completion, system_completion.id)
                            if not all([report_obj, head_obj, system_obj]):
                                err_ev = SSEEvent(
                                    event="completion.error",
                                    completion_id=str(system_completion.id),
                                    data={"result_id": str(r.id), "error": "Failed to initialize agent execution"},
                                )
                                await central_queue.put((str(r.id), err_ev))
                                return
                            # Build clients from report data sources
                            clients = {}
                            for data_source in getattr(report_obj, "data_sources", []):
                                try:
                                    ds_clients = await self.completions.data_source_service.construct_clients(session, data_source, current_user)
                                    clients.update(ds_clients)
                                except Exception:
                                    pass
                            # Pre-load files relationship in async context to avoid greenlet error in AgentV2.__init__
                            _ = getattr(report_obj, "files", [])
                            # Get build_id from run
                            build_id = str(run.build_id) if run.build_id else None
                            agent = AgentV2(
                                db=session,
                                organization=organization,
                                organization_settings=org_settings,
                                model=model,
                                small_model=small_model,
                                mode=prompt.mode,
                                report=report_obj,
                                messages=[],
                                head_completion=head_obj,
                                system_completion=system_obj,
                                widget=None,
                                step=None,
                                event_queue=eq,
                                clients=clients,
                                build_id=build_id,
                            )
                            await agent.main_execution()

                            # Multi-turn: run any follow-up turns on the same
                            # report before evaluating. The evaluator already
                            # scans the whole report, so global expectations
                            # cover the entire multi-turn trace.
                            prev_system = system_obj
                            case_row_multi = await session.get(TestCase, str(r.case_id))
                            additional_turns = list(
                                getattr(case_row_multi, "additional_turns_json", None) or []
                            )
                            for turn in additional_turns:
                                tp = (turn or {}).get("prompt") or {}
                                next_head = Completion(
                                    prompt={
                                        "content": tp.get("content") or "",
                                        "widget_id": None,
                                        "step_id": None,
                                        "mentions": tp.get("mentions"),
                                        "mode": tp.get("mode"),
                                        "model_id": tp.get("model_id"),
                                    },
                                    model=model.model_id,
                                    report_id=report_obj.id,
                                    parent_id=prev_system.id,
                                    turn_index=(prev_system.turn_index or 0) + 1,
                                    message_type="table",
                                    role="user",
                                    status="success",
                                    # Carry user_id from the original head so
                                    # downstream tools (e.g. create_artifact)
                                    # see a real user and don't violate
                                    # NOT NULL constraints.
                                    user_id=getattr(head, "user_id", None),
                                )
                                session.add(next_head)
                                await session.commit()
                                await session.refresh(next_head)

                                next_system = Completion(
                                    prompt=None,
                                    completion={"content": ""},
                                    model=model.model_id,
                                    report_id=report_obj.id,
                                    parent_id=next_head.id,
                                    turn_index=next_head.turn_index + 1,
                                    message_type="table",
                                    role="system",
                                    status="in_progress",
                                )
                                session.add(next_system)
                                await session.commit()
                                await session.refresh(next_system)

                                try:
                                    start_ev = SSEEvent(
                                        event="completion.started",
                                        completion_id=str(next_system.id),
                                        data={
                                            "result_id": str(r.id),
                                            "system_completion_id": str(next_system.id),
                                            "head_completion_id": str(next_head.id),
                                            "turn_index": next_head.turn_index,
                                        },
                                    )
                                    await central_queue.put((str(r.id), start_ev))
                                except Exception:
                                    pass

                                turn_agent = AgentV2(
                                    db=session,
                                    organization=organization,
                                    organization_settings=org_settings,
                                    model=model,
                                    small_model=small_model,
                                    mode=tp.get("mode"),
                                    report=report_obj,
                                    messages=[],
                                    head_completion=next_head,
                                    system_completion=next_system,
                                    widget=None,
                                    step=None,
                                    event_queue=eq,
                                    clients=clients,
                                    build_id=build_id,
                                )
                                await turn_agent.main_execution()
                                prev_system = next_system

                            # After final turn, evaluate assertions and persist TestResult
                            try:
                                # Resolve run/result/case/expectations
                                _run, result_row, case_row, expectations = await self.evaluator.resolve_by_run_and_report(
                                    session, str(run.id), str(report_obj.id)
                                )
                                # Build snapshot for assertions
                                snapshot = await self.evaluator.build_final_snapshot(session, str(report_obj.id))
                                # Prepare judge (optional)
                                try:
                                    judge = Judge(model=small_model, organization_settings=org_settings)
                                except Exception:
                                    judge = None
                                # Determine AgentExecution and duration
                                agent_execution_id = None
                                run_duration_ms = None
                                agent_meta: Dict[str, Any] = {}
                                try:
                                    res_exec = await session.execute(
                                        select(AgentExecution)
                                        .where(AgentExecution.completion_id == str(system_obj.id))
                                        .order_by(AgentExecution.created_at.desc())
                                        .limit(1)
                                    )
                                    ae = res_exec.scalar_one_or_none()
                                    if ae:
                                        agent_execution_id = str(ae.id)
                                        run_duration_ms = getattr(ae, "total_duration_ms", None)
                                        agent_meta = _agent_metadata_from_execution(ae)
                                        try:
                                            from app.models.plan_decision import PlanDecision as _PD
                                            n_iter = (await session.execute(
                                                select(func.count(_PD.id)).where(_PD.agent_execution_id == str(ae.id))
                                            )).scalar_one() or 0
                                            agent_meta["total_iterations"] = int(n_iter)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                # Case prompt text
                                case_prompt_text = ""
                                try:
                                    cj = getattr(case_row, "prompt_json", None) or {}
                                    case_prompt_text = cj.get("content") or ""
                                except Exception:
                                    case_prompt_text = ""
                                # Evaluate
                                status, result_json = await self.evaluator.evaluate_final(
                                    db=session,
                                    expectations=expectations,
                                    snapshot=snapshot,
                                    report_id=str(report_obj.id),
                                    case_prompt_text=case_prompt_text,
                                    judge=judge,
                                    organization=organization,
                                    current_user=current_user,
                                    run_duration_ms=run_duration_ms,
                                    agent_metadata=agent_meta,
                                )
                                # Persist
                                await self.evaluator.persist_result_json(
                                    db=session,
                                    result=result_row,
                                    status=status,
                                    result_json=result_json,
                                    failure_reason=None,
                                    agent_execution_id=agent_execution_id,
                                )
                                # Emit immediate UI update
                                try:
                                    payload = {
                                        "result_id": str(result_row.id),
                                        "status": status,
                                        "result_json": result_json.model_dump(),
                                    }
                                    await central_queue.put((str(result_row.id), SSEEvent(event="result.update", completion_id=str(system_completion.id), data=payload)))
                                except Exception:
                                    pass
                            except Exception as e:
                                # Best-effort: mark result as error
                                try:
                                    _run, result_row, _case_row, _expectations = await self.evaluator.resolve_by_run_and_report(
                                        session, str(run.id), str(report_obj.id)
                                    )
                                    # Snapshot real expectations into spec for error path
                                    try:
                                        rule_spec = RuleSpec(
                                            spec_version=getattr(_expectations, "spec_version", 1),
                                            rules=[(rr.model_dump() if hasattr(rr, "model_dump") else dict(rr)) for rr in (getattr(_expectations, "rules", []) or [])],
                                            order_mode=getattr(_expectations, "order_mode", None),
                                        )
                                    except Exception:
                                        rule_spec = RuleSpec(spec_version=1, rules=[], order_mode=None)
                                    await self.evaluator.persist_result_json(
                                        db=session,
                                        result=result_row,
                                        status="error",
                                        result_json=TestResultJsonSchema(
                                            spec=rule_spec,
                                            totals=TestResultTotals(total=0, passed=0, failed=0, duration_ms=None),
                                            rule_results=[],
                                        ),
                                        failure_reason=str(e),
                                        agent_execution_id=None,
                                    )
                                    try:
                                        payload = {
                                            "result_id": str(result_row.id),
                                            "status": "error",
                                            "failure_reason": str(e),
                                        }
                                        await central_queue.put((str(result_row.id), SSEEvent(event="result.update", completion_id=str(system_completion.id), data=payload)))
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                            finished_ev = SSEEvent(
                                event="completion.finished",
                                completion_id=str(system_completion.id),
                                data={"result_id": str(r.id), "status": "success"},
                            )
                            # Do not emit completion.finished here; rely on AgentV2 to emit it via event_queue.
                        except Exception as e:
                            err = SSEEvent(
                                event="completion.error",
                                completion_id=str(system_completion.id),
                                data={"result_id": str(r.id), "error": str(e)},
                            )
                            await central_queue.put((str(r.id), err))
                            # Persist result status=error so the TestResult
                            # row leaves `in_progress`. Without this, the
                            # streamer's aggregate run status is "success"
                            # (since no result is in {fail,error}) even
                            # though the agent failed. Use a fresh session
                            # because ``session`` may be in a tainted
                            # transactional state after main_execution raised.
                            try:
                                _error_async_session = create_async_session_factory()
                                async with _error_async_session() as _err_session:
                                    _run, result_row, _case_row, _expectations = await self.evaluator.resolve_by_run_and_report(
                                        _err_session, str(run.id), str(report_obj.id)
                                    )
                                    try:
                                        rule_spec = RuleSpec(
                                            spec_version=getattr(_expectations, "spec_version", 1),
                                            rules=[
                                                (rr.model_dump() if hasattr(rr, "model_dump") else dict(rr))
                                                for rr in (getattr(_expectations, "rules", []) or [])
                                            ],
                                            order_mode=getattr(_expectations, "order_mode", None),
                                        )
                                    except Exception:
                                        rule_spec = RuleSpec(spec_version=1, rules=[], order_mode=None)
                                    await self.evaluator.persist_result_json(
                                        db=_err_session,
                                        result=result_row,
                                        status="error",
                                        result_json=TestResultJsonSchema(
                                            spec=rule_spec,
                                            totals=TestResultTotals(total=0, passed=0, failed=0, duration_ms=None),
                                            rule_results=[],
                                        ),
                                        failure_reason=str(e),
                                        agent_execution_id=None,
                                    )
                            except Exception as _persist_err:
                                logging.warning(
                                    f"[stream_run] failed to persist error "
                                    f"status for result={r.id}: {_persist_err!r}"
                                )
                            # ALWAYS emit a terminal result.update, even if
                            # the DB persist above failed, so the streamer
                            # can mark this result finished and close the
                            # stream cleanly instead of hanging until the
                            # client times out.
                            try:
                                await central_queue.put((
                                    str(r.id),
                                    SSEEvent(
                                        event="result.update",
                                        completion_id=str(system_completion.id),
                                        data={
                                            "result_id": str(r.id),
                                            "status": "error",
                                            "failure_reason": str(e),
                                        },
                                    ),
                                ))
                            except Exception:
                                pass
                        finally:
                            eq.finish()

                # Start forwarder and runner
                asyncio.create_task(forward_events(str(r.id), eq))
                asyncio.create_task(run_agent_task())

        async def streamer():
            # Emit run.started
            start_payload = {
                "run_id": str(run.id),
                "results": [{"result_id": str(r.id), "report_id": str(r.report_id)} for r in results],
            }
            yield format_sse_event(SSEEvent(event="run.started", completion_id=str(run.id), data=start_payload))

            # Central queue for multiplexed events
            central_queue: "asyncio.Queue[tuple[str, SSEEvent]]" = asyncio.Queue()
            await start_pending_with_queues(central_queue)

            # Track terminal state based on completion.finished/error
            terminal = {"pass", "fail", "error", "stopped", "success"}
            finished: set[str] = set()
            # Seed finished with any results already in a terminal state
            try:
                for r in results:
                    if getattr(r, "status", "") in terminal:
                        finished.add(str(r.id))
            except Exception:
                pass
            total = len(results)
            # Emit loop: forward completion events and also mirror to result.update when status changes
            terminal = {"pass", "fail", "error", "stopped", "success"}
            import time as _time
            last_db_poll = 0.0
            while True:
                # Prefer event-driven; also periodically emit status updates
                try:
                    res_id, ev = await asyncio.wait_for(central_queue.get(), timeout=0.5)
                    # Forward completion.* events
                    yield format_sse_event(ev)
                    # A result is finished only when a terminal result.update
                    # arrives. completion.finished/error only says the agent
                    # loop is done — the evaluator still has to commit
                    # persist_result_json before the TestResult leaves
                    # in_progress. Closing the stream earlier would tear
                    # down the event loop under TestClient and leave the
                    # result permanently in_progress.
                    if ev.event == "result.update":
                        try:
                            st = None
                            if isinstance(ev.data, dict):
                                st = ev.data.get("status")
                            if st in terminal:
                                finished.add(res_id)
                        except Exception:
                            pass
                    if len(finished) >= total:
                        try:
                            finalized = await self._finalize_run_if_done(db, str(run.id))
                            await db.refresh(run)
                            if finalized is not None:
                                # e.g. a wake-armed run adopted via dedupe but
                                # driven to completion by this streamer.
                                asyncio.create_task(self._maybe_fire_wake(str(run.id)))
                        except Exception:
                            pass
                        yield format_sse_event(SSEEvent(event="run.finished", completion_id=str(run.id), data={"run_id": str(run.id), "status": run.status}))
                        break
                except asyncio.TimeoutError:
                    # Periodic DB status diff: the background finalizer (or
                    # another worker) may persist results this streamer never
                    # sees as events — poll every ~2s so live clients still
                    # observe those transitions and the stream terminates.
                    now = _time.monotonic()
                    if now - last_db_poll < 2.0:
                        continue
                    last_db_poll = now
                    try:
                        res_ref = await db.execute(
                            select(TestResult).where(TestResult.run_id == str(run.id)).execution_options(populate_existing=True)
                        )
                        for row in res_ref.scalars().all():
                            rid = str(row.id)
                            st = getattr(row, "status", "")
                            if result_id_to_status.get(rid) == st:
                                continue
                            result_id_to_status[rid] = st
                            payload: dict = {"result_id": rid, "status": st}
                            if st in terminal:
                                finished.add(rid)
                                rj = getattr(row, "result_json", None)
                                if isinstance(rj, dict) and rj:
                                    payload["result_json"] = rj
                                if getattr(row, "failure_reason", None):
                                    payload["failure_reason"] = row.failure_reason
                            yield format_sse_event(SSEEvent(event="result.update", completion_id=str(run.id), data=payload))
                        if len(finished) >= total:
                            try:
                                finalized = await self._finalize_run_if_done(db, str(run.id))
                                await db.refresh(run)
                                if finalized is not None:
                                    asyncio.create_task(self._maybe_fire_wake(str(run.id)))
                            except Exception:
                                pass
                            yield format_sse_event(SSEEvent(event="run.finished", completion_id=str(run.id), data={"run_id": str(run.id), "status": run.status}))
                            break
                    except Exception:
                        pass

        return StreamingResponse(streamer(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })


