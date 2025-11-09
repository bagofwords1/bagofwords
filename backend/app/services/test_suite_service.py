from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.models.test_suite import TestSuite
from fastapi import HTTPException
from app.schemas.test_expectations import TestCatalog, default_test_catalog
from app.services.llm_service import LLMService


class TestSuiteService:
    async def create_suite(self, db: AsyncSession, organization_id: str, current_user, name: str, description: Optional[str]) -> TestSuite:
        suite = TestSuite(
            organization_id=str(organization_id),
            name=name,
            description=description,
        )
        db.add(suite)
        await db.commit()
        await db.refresh(suite)
        return suite

    async def get_suite(self, db: AsyncSession, organization_id: str, current_user, suite_id: str) -> TestSuite:
        res = await db.execute(select(TestSuite).where(TestSuite.id == suite_id, TestSuite.organization_id == str(organization_id)))
        suite = res.scalar_one_or_none()
        if not suite:
            raise HTTPException(status_code=404, detail="Test suite not found")
        return suite

    async def list_suites(self, db: AsyncSession, organization_id: str, current_user, page: int = 1, limit: int = 20, search: Optional[str] = None) -> List[TestSuite]:
        stmt = select(TestSuite).where(TestSuite.organization_id == str(organization_id))
        if search:
            from sqlalchemy import or_
            like = f"%{search}%"
            stmt = stmt.where(or_(TestSuite.name.ilike(like), TestSuite.description.ilike(like)))
        stmt = stmt.order_by(TestSuite.created_at.desc()).offset((page - 1) * limit).limit(limit)
        res = await db.execute(stmt)
        return res.scalars().all()

    async def update_suite(self, db: AsyncSession, organization_id: str, current_user, suite_id: str, name: Optional[str], description: Optional[str]) -> TestSuite:
        suite = await self.get_suite(db, organization_id, current_user, suite_id)
        if name is not None:
            suite.name = name
        if description is not None:
            suite.description = description
        db.add(suite)
        await db.commit()
        await db.refresh(suite)
        return suite

    async def delete_suite(self, db: AsyncSession, organization_id: str, current_user, suite_id: str) -> None:
        suite = await self.get_suite(db, organization_id, current_user, suite_id)
        await db.delete(suite)
        await db.commit()


    async def get_test_catalog(self, db: AsyncSession, organization_id: str, current_user) -> TestCatalog:
        """Return the curated test catalog for building expectations in the UI.

        For MVP this is static; later we can tailor per-organization and tool availability.
        """
        catalog = default_test_catalog()

        # Populate Judge (LLM) model options dynamically from available models
        try:
            llm_service = LLMService()
            class _Org:
                def __init__(self, id: str):
                    self.id = id
            models = await llm_service.get_models(db, organization=_Org(organization_id), current_user=current_user, is_enabled=True)
            # Build options: label as "<Provider> — <Model Name>", value as model_id
            model_options = []
            # Prefer small default first, then regular default, then alphabetically
            def _sort_key(m):
                try:
                    provider_name = getattr(getattr(m, 'provider', None), 'name', '') or ''
                except Exception:
                    provider_name = ''
                model_name = getattr(m, 'name', None) or getattr(m, 'model_id', '')
                return (
                    0 if getattr(m, 'is_small_default', False) else 1,
                    0 if getattr(m, 'is_default', False) else 1,
                    provider_name.lower(),
                    str(model_name).lower(),
                )
            for m in sorted(models or [], key=_sort_key):
                try:
                    provider_name = getattr(getattr(m, 'provider', None), 'name', None) or getattr(m, 'provider', None) or ''
                except Exception:
                    provider_name = ''
                model_name = getattr(m, 'name', None) or getattr(m, 'model_id', '')
                model_id = getattr(m, 'model_id', '')
                label = f"{provider_name} — {model_name}".strip(" —")
                model_options.append({ 'label': label, 'value': model_id })

            for cat in catalog.categories:
                if cat.id == 'judge':
                    for f in cat.fields:
                        if f.key == 'model_id':
                            f.options = model_options
                            # include top examples to guide UI when needed
                            f.examples = [opt['value'] for opt in model_options[:3]] if model_options else []
                    break
        except Exception:
            # If anything fails, fall back to static catalog without options
            pass

        return catalog


