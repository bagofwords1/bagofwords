from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from typing import List, Optional, Sequence
from fastapi import HTTPException

from app.models.eval import TestCase, TestSuite
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider


class TestCaseService:
    async def _get_suite(self, db: AsyncSession, organization_id: str, current_user, suite_id: str) -> TestSuite:
        res = await db.execute(select(TestSuite).where(TestSuite.id == suite_id, TestSuite.organization_id == str(organization_id)))
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
        res = await db.execute(select(TestCase).where(TestCase.id == case_id))
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
        res = await db.execute(select(TestCase).where(TestCase.suite_id == str(suite_id)).order_by(TestCase.created_at.asc()))
        return res.scalars().all()

    async def update_case(self, db: AsyncSession, organization_id: str, current_user, case_id: str, name: Optional[str], prompt_json: Optional[dict], expectations_json, data_source_ids_json: Optional[list]) -> TestCase:
        case = await self.get_case(db, organization_id, current_user, case_id)
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

    async def delete_case(self, db: AsyncSession, organization_id: str, current_user, case_id: str) -> None:
        case = await self.get_case(db, organization_id, current_user, case_id)
        await db.delete(case)
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
    ) -> List[TestCase]:
        """List cases across suites with optional filters. Joins through TestSuite to enforce org scope.

        Note: This method is provided for future endpoints and UI filters; current UI composes
        per-suite requests client-side. Searching matches against TestCase.name and a coarse
        string match on prompt_json (DB-dependent JSON LIKE behavior).
        """
        # Ensure suites belong to org when filtering
        if suite_ids:
            # Validate all suites
            for sid in suite_ids:
                await self._get_suite(db, organization_id, current_user, str(sid))
        # Base: suites in org
        from sqlalchemy import cast, String
        stmt = select(TestCase).join(TestSuite, TestCase.suite_id == TestSuite.id).where(
            TestSuite.organization_id == str(organization_id)
        )
        if suite_ids:
            stmt = stmt.where(TestCase.suite_id.in_([str(s) for s in suite_ids]))
        if search:
            like = f"%{search}%"
            # Coarse match on prompt_json string representation; portable enough for SQLite/postgres
            stmt = stmt.where(or_(TestCase.name.ilike(like), cast(TestCase.prompt_json, String).ilike(like)))
        stmt = stmt.order_by(TestCase.created_at.desc()).offset((page - 1) * limit).limit(limit)
        res = await db.execute(stmt)
        return res.scalars().all()
