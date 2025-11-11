from typing import Optional, List, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException
from datetime import datetime
import uuid

from app.models.test_suite import TestSuite, TestCase, TestRun, TestResult
from app.models.report import Report
from app.models.completion import Completion
from app.services.completion_service import CompletionService
from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
from app.schemas.test_dashboard_schema import TestMetricsSchema, TestSuiteSummarySchema
from app.streaming.completion_stream import CompletionEventQueue
from app.settings.database import create_async_session_factory
from app.ai.agent_v2 import AgentV2


class TestRunService:
    def __init__(self) -> None:
        self.completions = CompletionService()

    # -------- Helpers --------
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

    async def _get_cases(self, db: AsyncSession, suite_id: str) -> List[TestCase]:
        res = await db.execute(select(TestCase).where(TestCase.suite_id == str(suite_id)).order_by(TestCase.created_at.asc()))
        return res.scalars().all()

    async def _create_stub_report(self, db: AsyncSession, organization_id: str, user_id: str, title: str) -> Report:
        slug = f"testrun-{uuid.uuid4().hex[:12]}"
        report = Report(
            title=title,
            slug=slug,
            status="draft",
            report_type="regular",
            user_id=user_id,
            organization_id=organization_id,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
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
            model = await organization.get_default_llm_model(db)
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

    async def create_run(self, db: AsyncSession, organization, current_user, case_ids: Optional[List[str]] = None, trigger_reason: Optional[str] = "manual") -> TestRun:
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

        # Create run
        run = TestRun(
            suite_ids=suite_ids_str,
            requested_by_user_id=str(current_user.id) if current_user else None,
            trigger_reason=trigger_reason or "manual",
            status="in_progress",
            started_at=datetime.utcnow(),
            title=title,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # Create placeholder TestResult per case (with stub report + head completion)
        for case in cases:
            report_title = f"Test Run · {case.name}"
            report = await self._create_stub_report(db, str(organization.id), str(current_user.id), report_title)
            head = await self._create_head_completion(db, str(report.id), organization, current_user, prompt=case.prompt_json or {})

            result = TestResult(
                run_id=str(run.id),
                case_id=str(case.id),
                head_completion_id=str(head.id),
                status="init",
                report_id=str(report.id),
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
        return res.scalars().all()

    async def get_result(self, db: AsyncSession, organization_id: str, current_user, result_id: str) -> TestResult:
        res = await db.execute(select(TestResult).where(TestResult.id == result_id))
        result = res.scalar_one_or_none()
        if not result:
            raise HTTPException(status_code=404, detail="Test result not found")
        # ensure run -> suite in org
        _ = await self.get_run(db, organization_id, current_user, str(result.run_id))
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

        # TODO: do sigkill to all the completions in the run
        
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
        await db.refresh(run)
        return run

    # ---- Dashboard helpers (mock data for MVP) ----
    async def get_dashboard_metrics(self, db: AsyncSession, organization_id: str, current_user) -> TestMetricsSchema:
        # Mock: count total test cases and estimate success_rate
        res = await db.execute(select(TestCase).join(TestSuite, TestCase.suite_id == TestSuite.id).where(TestSuite.organization_id == str(organization_id)))
        total_cases = len(res.scalars().all())
        # Mock success rate: 0.75 if cases exist, else 0.0
        success_rate = 0.75 if total_cases > 0 else 0.0
        return TestMetricsSchema(total_tests=total_cases, success_rate=success_rate)

    async def get_suites_summary(self, db: AsyncSession, organization_id: str, current_user) -> List[TestSuiteSummarySchema]:
        # Return suites with mock counts and last run info
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
            # mock pass_rate per suite
            pass_rate = 0.8 if tests_count > 0 else 0.0
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
    async def create_and_execute_background(self, db: AsyncSession, organization, current_user, case_ids: Optional[List[str]] = None, suite_id: Optional[str] = None, trigger_reason: Optional[str] = "manual") -> tuple[TestRun, List[TestResult]]:
        # Resolve cases from inputs
        cases = await self._resolve_cases_inputs(db, str(organization.id), case_ids, suite_id)
        if not cases:
            raise HTTPException(status_code=400, detail="No test cases found")

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
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        created_results: List[TestResult] = []

        # For each case: create a report and kick off background completion via CompletionService
        from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
        for case in cases:
            report_title = f"Test Run · {case.name}"
            report = await self._create_stub_report(db, str(organization.id), str(current_user.id), report_title)

            # Build prompt schema from case
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

            # Create head+system and run agent in background using existing service
            v2 = await self.completions.create_completion(
                db=db,
                report_id=str(report.id),
                completion_data=completion_data,
                current_user=current_user,
                organization=organization,
                background=True,
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

            result = TestResult(
                run_id=str(run.id),
                case_id=str(case.id),
                head_completion_id=str(head_id) if head_id else str(uuid.uuid4()),  # fallback placeholder
                status="in_progress",
                report_id=str(report.id),
            )
            db.add(result)
            created_results.append(result)

        await db.commit()
        # refresh results to include IDs
        for r in created_results:
            await db.refresh(r)

        return run, created_results

    # -------- New API: Run status with embedded completions (polling) --------
    async def get_run_status_with_completions(self, db: AsyncSession, organization, current_user, run_id: str, limit: int = 50):
        # Load run and validate
        run = await self.get_run(db, str(organization.id), current_user, run_id)
        # Get all results
        res = await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)).order_by(TestResult.created_at.asc()))
        results = res.scalars().all()

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
        # Delegate to existing streaming method (creates head+system and streams AgentV2)
        return await self.completions.create_completion_stream(
            db=db,
            report_id=str(result.report_id),
            completion_data=completion_data,
            current_user=current_user,
            organization=organization,
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
                if getattr(r, "status", "") != "init":
                    continue

                # Load case + head completion
                res_case = await db.execute(select(TestCase).where(TestCase.id == str(r.case_id)))
                case = res_case.scalar_one_or_none()
                if not case:
                    continue
                head = await db.get(Completion, str(r.head_completion_id))
                if not head:
                    continue

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
                    model = await organization.get_default_llm_model(db)
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

                # Create system completion linked to the existing head
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
                                    clients[data_source.name] = await self.completions.data_source_service.construct_client(session, data_source, current_user)
                                except Exception:
                                    pass
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
                            )
                            await agent.main_execution()
                            finished_ev = SSEEvent(
                                event="completion.finished",
                                completion_id=str(system_completion.id),
                                data={"result_id": str(r.id), "status": "success"},
                            )
                            await central_queue.put((str(r.id), finished_ev))
                        except Exception as e:
                            err = SSEEvent(
                                event="completion.error",
                                completion_id=str(system_completion.id),
                                data={"result_id": str(r.id), "error": str(e)},
                            )
                            await central_queue.put((str(r.id), err))
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
            total = len(results)
            # Emit loop: forward completion events and also mirror to result.update when status changes
            terminal = {"pass", "fail", "error", "stopped", "success"}
            while True:
                # Prefer event-driven; also periodically emit status updates
                try:
                    res_id, ev = await asyncio.wait_for(central_queue.get(), timeout=0.5)
                    # Forward completion.* events
                    yield format_sse_event(ev)
                    if ev.event in ("completion.finished", "completion.error"):
                        finished.add(res_id)
                        # Mirror to result.update by reloading status from DB
                        try:
                            rdb = await db.get(TestResult, res_id)
                            if rdb:
                                payload = {
                                    "result_id": res_id,
                                    "status": getattr(rdb, "status", None),
                                }
                                # Include result_json if present
                                try:
                                    if getattr(rdb, "result_json", None) is not None:
                                        payload["result_json"] = getattr(rdb, "result_json")
                                except Exception:
                                    pass
                                yield format_sse_event(SSEEvent(event="result.update", completion_id=res_id, data=payload))
                        except Exception:
                            pass
                    if len(finished) >= total:
                        try:
                            run.finished_at = run.finished_at or datetime.utcnow()
                            # Approximate aggregate
                            res_ref = await db.execute(select(TestResult).where(TestResult.run_id == str(run.id)))
                            rows = res_ref.scalars().all()
                            statuses = [getattr(x, "status", "") for x in rows]
                            run.status = "success" if all(s not in {"fail", "error"} for s in statuses) else "error"
                            db.add(run)
                            await db.commit()
                        except Exception:
                            pass
                        yield format_sse_event(SSEEvent(event="run.finished", completion_id=str(run.id), data={"run_id": str(run.id), "status": run.status}))
                        break
                except asyncio.TimeoutError:
                    # Periodic status diff (optional)
                    pass

        return StreamingResponse(streamer(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        })


