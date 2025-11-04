from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.models.test_suite import TestSuite
from app.models.report import Report
from fastapi import HTTPException


class TestSuiteService:
    async def create_suite(self, db: AsyncSession, organization_id: str, name: str, description: Optional[str], report_id: str) -> TestSuite:
        report = await db.get(Report, report_id)
        if not report or str(report.organization_id) != str(organization_id) or getattr(report, 'report_type', 'regular') != 'test':
            raise HTTPException(status_code=400, detail="report_id must reference a test report in this organization")

        suite = TestSuite(
            organization_id=str(organization_id),
            name=name,
            description=description,
            report_id=str(report_id),
        )
        db.add(suite)
        await db.commit()
        await db.refresh(suite)
        return suite

    async def get_suite(self, db: AsyncSession, organization_id: str, suite_id: str) -> TestSuite:
        res = await db.execute(select(TestSuite).where(TestSuite.id == suite_id, TestSuite.organization_id == str(organization_id)))
        suite = res.scalar_one_or_none()
        if not suite:
            raise HTTPException(status_code=404, detail="Test suite not found")
        return suite

    async def list_suites(self, db: AsyncSession, organization_id: str, page: int = 1, limit: int = 20, search: Optional[str] = None) -> List[TestSuite]:
        stmt = select(TestSuite).where(TestSuite.organization_id == str(organization_id))
        if search:
            from sqlalchemy import or_
            like = f"%{search}%"
            stmt = stmt.where(or_(TestSuite.name.ilike(like), TestSuite.description.ilike(like)))
        stmt = stmt.order_by(TestSuite.created_at.desc()).offset((page - 1) * limit).limit(limit)
        res = await db.execute(stmt)
        return res.scalars().all()

    async def update_suite(self, db: AsyncSession, organization_id: str, suite_id: str, name: Optional[str], description: Optional[str], report_id: Optional[str]) -> TestSuite:
        suite = await self.get_suite(db, organization_id, suite_id)
        if name is not None:
            suite.name = name
        if description is not None:
            suite.description = description
        if report_id is not None:
            report = await db.get(Report, report_id)
            if not report or str(report.organization_id) != str(organization_id) or getattr(report, 'report_type', 'regular') != 'test':
                raise HTTPException(status_code=400, detail="report_id must reference a test report in this organization")
            suite.report_id = str(report_id)
        db.add(suite)
        await db.commit()
        await db.refresh(suite)
        return suite

    async def delete_suite(self, db: AsyncSession, organization_id: str, suite_id: str) -> None:
        suite = await self.get_suite(db, organization_id, suite_id)
        await db.delete(suite)
        await db.commit()


