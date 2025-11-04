from typing import Optional, List, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from app.models.test_suite import TestSuite, TestCase, TestRun, TestResult
from app.models.report import Report
from app.services.completion_service import CompletionService
from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema


class TestRunService:
    def __init__(self) -> None:
        self.completions = CompletionService()

    async def _get_suite(self, db: AsyncSession, organization_id: str, suite_id: str) -> TestSuite:
        res = await db.execute(select(TestSuite).where(TestSuite.id == suite_id, TestSuite.organization_id == str(organization_id)))
        suite = res.scalar_one_or_none()
        if not suite:
            raise HTTPException(status_code=404, detail="Test suite not found")
        return suite

    async def _get_cases(self, db: AsyncSession, suite_id: str) -> List[TestCase]:
        res = await db.execute(select(TestCase).where(TestCase.suite_id == str(suite_id)).order_by(TestCase.created_at.asc()))
        return res.scalars().all()

    async def run_suite(self, db: AsyncSession, organization, current_user, suite_id: str, background: bool = True) -> TestRun:
        suite = await self._get_suite(db, str(organization.id), suite_id)
        report = await db.get(Report, str(suite.report_id))
        if not report or getattr(report, 'report_type', 'regular') != 'test' or str(report.organization_id) != str(organization.id):
            raise HTTPException(status_code=400, detail="Suite is not bound to a valid test report in this organization")

        run = TestRun(
            suite_id=str(suite.id),
            requested_by_user_id=str(current_user.id),
            trigger_reason="manual",
            status="in_progress",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        cases = await self._get_cases(db, str(suite.id))
        if not cases:
            run.status = "error"
            run.summary_json = {"passed": 0, "failed": 0, "total": 0, "reason": "no_cases"}
            db.add(run)
            await db.commit()
            await db.refresh(run)
            return run

        # Foreground for MVP
        passed = 0
        failed = 0
        for case in cases:
            # Create result row
            tr = TestResult(run_id=str(run.id), case_id=str(case.id), status="in_progress")
            db.add(tr)
            await db.commit(); await db.refresh(tr)

            try:
                pj = case.prompt_json or {}
                prompt = CompletionCreate(prompt=PromptSchema(**{
                    "content": pj.get("content") or pj.get("text") or "",
                    "widget_id": pj.get("widget_id"),
                    "step_id": pj.get("step_id"),
                    "mentions": pj.get("mentions"),
                    "mode": pj.get("mode") or "chat",
                    "model_id": pj.get("model_id"),
                }))

                # Foreground agent execution (non-stream), reuse existing service
                v2 = await self.completions.create_completion(
                    db=db,
                    report_id=str(report.id),
                    completion_data=prompt,
                    current_user=current_user,
                    organization=organization,
                    background=False,
                )

                # Extract head/system ids from response
                head_id = None
                system_id = None
                agent_execution_id = None
                for c in v2.completions:
                    if c.role == 'system':
                        system_id = c.id
                        agent_execution_id = c.agent_execution_id
                    else:
                        head_id = c.id

                if not head_id:
                    raise RuntimeError("Head completion not found")

                tr.head_completion_id = str(head_id)
                tr.agent_execution_id = str(agent_execution_id) if agent_execution_id else None

                # For MVP, mark pass (assertions will be added next phases)
                tr.status = "pass"
                db.add(tr)
                await db.commit(); await db.refresh(tr)
                passed += 1
            except Exception as e:
                tr.status = "error"
                tr.failure_reason = str(e)
                db.add(tr)
                await db.commit(); await db.refresh(tr)
                failed += 1

        run.status = "success" if failed == 0 else "error"
        run.summary_json = {"passed": passed, "failed": failed, "total": passed + failed}
        db.add(run)
        await db.commit(); await db.refresh(run)
        return run

    async def get_run(self, db: AsyncSession, organization_id: str, run_id: str) -> TestRun:
        res = await db.execute(select(TestRun).where(TestRun.id == run_id))
        run = res.scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Test run not found")
        # Ensure suite belongs to org
        _ = await self._get_suite(db, organization_id, str(run.suite_id))
        return run

    async def list_runs(self, db: AsyncSession, organization_id: str, suite_id: Optional[str] = None, status: Optional[str] = None, page: int = 1, limit: int = 20) -> List[TestRun]:
        from app.models.test_suite import TestSuite
        stmt = select(TestRun)
        if suite_id:
            # also ensure suite in org
            await self._get_suite(db, organization_id, suite_id)
            stmt = stmt.where(TestRun.suite_id == str(suite_id))
        stmt = stmt.order_by(TestRun.created_at.desc()).offset((page - 1) * limit).limit(limit)
        res = await db.execute(stmt)
        return res.scalars().all()

    async def list_results(self, db: AsyncSession, organization_id: str, run_id: str) -> List[TestResult]:
        _ = await self.get_run(db, organization_id, run_id)
        res = await db.execute(select(TestResult).where(TestResult.run_id == str(run_id)).order_by(TestResult.created_at.asc()))
        return res.scalars().all()

    async def get_result(self, db: AsyncSession, organization_id: str, result_id: str) -> TestResult:
        res = await db.execute(select(TestResult).where(TestResult.id == result_id))
        result = res.scalar_one_or_none()
        if not result:
            raise HTTPException(status_code=404, detail="Test result not found")
        # ensure run -> suite in org
        _ = await self.get_run(db, organization_id, str(result.run_id))
        return result


