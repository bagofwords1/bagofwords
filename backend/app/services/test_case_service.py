from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from typing import List, Optional, Sequence
from datetime import datetime
from fastapi import HTTPException

from app.models.eval import (
    TestCase,
    TestSuite,
    TEST_CASE_STATUSES,
    DEFAULT_DRAFTS_SUITE_NAME,
)
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider
from app.core.eval_scope import agent_scope_clause


class TestCaseService:
    async def _get_suite(self, db: AsyncSession, organization_id: str, current_user, suite_id: str) -> TestSuite:
        res = await db.execute(
            select(TestSuite)
            .where(TestSuite.id == suite_id, TestSuite.organization_id == str(organization_id))
            .where(TestSuite.deleted_at.is_(None))
        )
        suite = res.scalar_one_or_none()
        if not suite:
            raise HTTPException(status_code=404, detail="Test suite not found")
        return suite

    async def create_case(self, db: AsyncSession, organization_id: str, current_user, suite_id: str, name: str, prompt_json: dict, expectations_json, data_source_ids_json: Optional[list] = None) -> TestCase:
        await self._get_suite(db, organization_id, current_user, suite_id)
        if not isinstance(prompt_json, dict) or not (prompt_json.get("content") or prompt_json.get("text")):
            raise HTTPException(status_code=400, detail="prompt_json.content is required")
        # Coerce Pydantic models to plain dicts for JSON column storage
        if hasattr(expectations_json, "model_dump"):
            try:
                expectations_json = expectations_json.model_dump()
            except Exception:
                expectations_json = dict(expectations_json)
        tc = TestCase(
            suite_id=str(suite_id),
            name=name,
            prompt_json=prompt_json,
            expectations_json=expectations_json or {},
            data_source_ids_json=data_source_ids_json or [],
        )
        db.add(tc)
        await db.commit()
        await db.refresh(tc)
        return tc

    async def get_case(self, db: AsyncSession, organization_id: str, current_user, case_id: str) -> TestCase:
        res = await db.execute(
            select(TestCase)
            .where(TestCase.id == case_id)
            .where(TestCase.deleted_at.is_(None))
        )
        case = res.scalar_one_or_none()
        if not case:
            raise HTTPException(status_code=404, detail="Test case not found")
        # ensure org
        await self._get_suite(db, organization_id, current_user, str(case.suite_id))
        # Enrich with derived model summary for UI fallbacks
        try:
            model_ref = None
            pj = case.prompt_json or {}
            model_ref = str(pj.get("model_id") or "")
            if model_ref:
                q = (
                    select(LLMModel, LLMProvider)
                    .join(LLMModel.provider)
                    .where(LLMModel.organization_id == str(organization_id))
                    .where(or_(LLMModel.id == model_ref, LLMModel.model_id == model_ref))
                )
                res2 = await db.execute(q)
                row = res2.first()
                if row:
                    model, provider = row
                    setattr(case, "model_summary", {
                        "id": str(model.id),
                        "model_id": model.model_id,
                        "name": model.name,
                        "provider_name": provider.name,
                        "provider_type": provider.provider_type
                    })
        except Exception:
            # Best-effort enrichment; ignore failures
            pass
        return case

    async def list_cases(self, db: AsyncSession, organization_id: str, current_user, suite_id: str) -> List[TestCase]:
        await self._get_suite(db, organization_id, current_user, suite_id)
        res = await db.execute(
            select(TestCase)
            .where(TestCase.suite_id == str(suite_id))
            .where(TestCase.deleted_at.is_(None))
            .order_by(TestCase.created_at.asc())
        )
        return res.scalars().all()

    async def update_case(self, db: AsyncSession, organization_id: str, current_user, case_id: str, name: Optional[str], prompt_json: Optional[dict], expectations_json, data_source_ids_json: Optional[list], suite_id: Optional[str] = None) -> TestCase:
        case = await self.get_case(db, organization_id, current_user, case_id)
        if suite_id is not None and str(suite_id) != str(case.suite_id):
            # Resolve through the org-scoped lookup so a case cannot be filed
            # into another organization's suite.
            target = (await db.execute(
                select(TestSuite).where(
                    TestSuite.id == str(suite_id),
                    TestSuite.organization_id == str(organization_id),
                    TestSuite.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if target is None:
                raise HTTPException(status_code=404, detail="Test suite not found")
            case.suite_id = str(target.id)
        if name is not None:
            case.name = name
        if prompt_json is not None:
            if not isinstance(prompt_json, dict) or not (prompt_json.get("content") or prompt_json.get("text")):
                raise HTTPException(status_code=400, detail="prompt_json.content is required")
            case.prompt_json = prompt_json
        if expectations_json is not None:
            if hasattr(expectations_json, "model_dump"):
                try:
                    expectations_json = expectations_json.model_dump()
                except Exception:
                    expectations_json = dict(expectations_json)
            case.expectations_json = expectations_json
        if data_source_ids_json is not None:
            case.data_source_ids_json = data_source_ids_json
        db.add(case)
        await db.commit()
        await db.refresh(case)
        return case

    async def update_case_status(
        self,
        db: AsyncSession,
        organization_id: str,
        current_user,
        case_id: str,
        status: str,
    ) -> TestCase:
        """Promote a draft to active or archive any case. Org-scoped via the
        suite check inside ``get_case``.
        """
        if status not in TEST_CASE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Allowed: {sorted(TEST_CASE_STATUSES)}",
            )
        case = await self.get_case(db, organization_id, current_user, case_id)
        case.status = status
        db.add(case)
        await db.commit()
        await db.refresh(case)
        return case

    async def get_or_create_drafts_suite(
        self,
        db: AsyncSession,
        organization_id: str,
        suite_name: str = DEFAULT_DRAFTS_SUITE_NAME,
        data_source_id: str | None = None,
    ) -> TestSuite:
        """Find-or-create a default drafts suite.

        With ``data_source_id`` this is that AGENT's drafts bucket; without it,
        the org-wide one. Per-agent buckets exist because a single shared Drafts
        collects every agent's auto-drafted cases, which in a large org is both
        an unusable pile and — since running a suite needs authority over every
        case in it — a suite nobody but an org admin can run.

        Created LAZILY, on the first case that needs it. Creating one per agent
        up front would put an empty folder per agent in every suite list, which
        in an org with thousands of agents is worse than the problem.

        Idempotent at the service layer — there is no DB unique constraint on
        ``(organization_id, data_source_id, name)``, but in steady state there
        is at most one row per (org, agent).
        """
        stmt = (
            select(TestSuite)
            .where(TestSuite.organization_id == str(organization_id))
            .where(TestSuite.name == suite_name)
            .where(TestSuite.deleted_at.is_(None))
            .where(
                TestSuite.data_source_id == str(data_source_id)
                if data_source_id
                else TestSuite.data_source_id.is_(None)
            )
            .order_by(TestSuite.created_at.asc())
            .limit(1)
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        suite = TestSuite(
            organization_id=str(organization_id),
            name=suite_name,
            data_source_id=str(data_source_id) if data_source_id else None,
            description=(
                "Default bucket for auto-drafted and unscoped eval cases. "
                "Drafts here are excluded from scheduled runs — promote to "
                "active to include them."
            ),
        )
        db.add(suite)
        await db.commit()
        await db.refresh(suite)
        return suite

    async def delete_case(self, db: AsyncSession, organization_id: str, current_user, case_id: str) -> None:
        # Soft delete: TestResult rows keep case_id as an FK target, so a
        # hard DELETE fails with a foreign-key violation once the case has
        # been part of any run.
        case = await self.get_case(db, organization_id, current_user, case_id)
        case.deleted_at = datetime.utcnow()
        db.add(case)
        await db.commit()

    async def list_cases_multi(
        self,
        db: AsyncSession,
        organization_id: str,
        current_user,
        suite_ids: Optional[Sequence[str]] = None,
        search: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
        data_source_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> List[TestCase]:
        """List cases across suites with optional filters. Joins through TestSuite to enforce org scope.

        Searching matches against TestCase.name and a coarse string match on
        prompt_json (DB-dependent JSON LIKE behavior).

        ``data_source_id`` / ``scope`` narrow to one agent BEFORE the limit is
        applied, so the page is that agent's newest cases rather than the org's
        newest cases that happen to include it.
        """
        # Ensure suites belong to org when filtering
        if suite_ids:
            # Validate all suites
            for sid in suite_ids:
                await self._get_suite(db, organization_id, current_user, str(sid))
        # Base: suites in org
        from sqlalchemy import cast, String
        stmt = select(TestCase).join(TestSuite, TestCase.suite_id == TestSuite.id).where(
            TestSuite.organization_id == str(organization_id),
            TestCase.deleted_at.is_(None),
        )
        if suite_ids:
            stmt = stmt.where(TestCase.suite_id.in_([str(s) for s in suite_ids]))
        if search:
            like = f"%{search}%"
            # Coarse match on prompt_json string representation; portable enough for SQLite/postgres
            stmt = stmt.where(or_(TestCase.name.ilike(like), cast(TestCase.prompt_json, String).ilike(like)))
        agent_clause = agent_scope_clause(TestCase.data_source_ids_json, data_source_id, scope)
        if agent_clause is not None:
            stmt = stmt.where(agent_clause)
        stmt = stmt.order_by(TestCase.created_at.desc()).offset((page - 1) * limit).limit(limit)
        res = await db.execute(stmt)
        return res.scalars().all()
