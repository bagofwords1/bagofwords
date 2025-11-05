from typing import Optional, List, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from app.models.test_suite import TestSuite, TestCase, TestRun, TestResult
from app.models.report import Report
from app.services.completion_service import CompletionService
from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
from app.schemas.test_dashboard_schema import TestMetricsSchema, TestSuiteSummarySchema


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
        # Report association will be added later; block for now
        raise HTTPException(status_code=400, detail="Test runs are not configured yet for this suite")

        # The below implementation will be re-enabled once report linkage is added

    async def get_run(self, db: AsyncSession, organization_id: str, current_user, run_id: str) -> TestRun:
        res = await db.execute(select(TestRun).where(TestRun.id == run_id))
        run = res.scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Test run not found")
        # Ensure suite belongs to org
        _ = await self._get_suite(db, organization_id, str(current_user.id), str(run.suite_id))
        return run

    async def list_runs(self, db: AsyncSession, organization_id: str, current_user, suite_id: Optional[str] = None, status: Optional[str] = None, page: int = 1, limit: int = 20) -> List[TestRun]:
        from app.models.test_suite import TestSuite
        stmt = select(TestRun)
        if suite_id:
            # also ensure suite in org
            await self._get_suite(db, organization_id, suite_id)
            stmt = stmt.where(TestRun.suite_id == str(suite_id))
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
            # last run (mock by picking latest TestRun if exists)
            res_run = await db.execute(select(TestRun).where(TestRun.suite_id == str(s.id)).order_by(TestRun.created_at.desc()).limit(1))
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


