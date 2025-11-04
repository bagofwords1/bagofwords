from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from fastapi import HTTPException

from app.models.test_suite import TestCase, TestSuite


class TestCaseService:
    async def _get_suite(self, db: AsyncSession, organization_id: str, suite_id: str) -> TestSuite:
        res = await db.execute(select(TestSuite).where(TestSuite.id == suite_id, TestSuite.organization_id == str(organization_id)))
        suite = res.scalar_one_or_none()
        if not suite:
            raise HTTPException(status_code=404, detail="Test suite not found")
        return suite

    async def create_case(self, db: AsyncSession, organization_id: str, suite_id: str, name: str, prompt_json: dict, expectations_json: dict, data_source_ids_json: Optional[list] = None) -> TestCase:
        await self._get_suite(db, organization_id, suite_id)
        if not isinstance(prompt_json, dict) or not (prompt_json.get("content") or prompt_json.get("text")):
            raise HTTPException(status_code=400, detail="prompt_json.content is required")
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

    async def get_case(self, db: AsyncSession, organization_id: str, case_id: str) -> TestCase:
        res = await db.execute(select(TestCase).where(TestCase.id == case_id))
        case = res.scalar_one_or_none()
        if not case:
            raise HTTPException(status_code=404, detail="Test case not found")
        # ensure org
        await self._get_suite(db, organization_id, str(case.suite_id))
        return case

    async def list_cases(self, db: AsyncSession, organization_id: str, suite_id: str) -> List[TestCase]:
        await self._get_suite(db, organization_id, suite_id)
        res = await db.execute(select(TestCase).where(TestCase.suite_id == str(suite_id)).order_by(TestCase.created_at.asc()))
        return res.scalars().all()

    async def update_case(self, db: AsyncSession, organization_id: str, case_id: str, name: Optional[str], prompt_json: Optional[dict], expectations_json: Optional[dict], data_source_ids_json: Optional[list]) -> TestCase:
        case = await self.get_case(db, organization_id, case_id)
        if name is not None:
            case.name = name
        if prompt_json is not None:
            if not isinstance(prompt_json, dict) or not (prompt_json.get("content") or prompt_json.get("text")):
                raise HTTPException(status_code=400, detail="prompt_json.content is required")
            case.prompt_json = prompt_json
        if expectations_json is not None:
            case.expectations_json = expectations_json
        if data_source_ids_json is not None:
            case.data_source_ids_json = data_source_ids_json
        db.add(case)
        await db.commit()
        await db.refresh(case)
        return case

    async def delete_case(self, db: AsyncSession, organization_id: str, case_id: str) -> None:
        case = await self.get_case(db, organization_id, case_id)
        await db.delete(case)
        await db.commit()


