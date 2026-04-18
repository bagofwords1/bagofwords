from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime

import yaml
from fastapi import HTTPException

from app.models.eval import TestSuite, TestCase
from app.models.data_source import DataSource
from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider
from app.schemas.test_expectations import TestCatalog, default_test_catalog
from app.schemas.suite_yaml_schema import (
    SuiteYaml,
    CaseYaml,
    PromptYaml,
    TurnYaml,
    merge_tags,
)
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

    async def ensure_default_for_org(self, db: AsyncSession, organization_id: str, current_user) -> Optional[TestSuite]:
        """Create a single default suite for the organization if none exist.

        Idempotent: if the org already has any suite, does nothing.
        """
        res = await db.execute(select(TestSuite.id).where(TestSuite.organization_id == str(organization_id)))
        existing = res.scalars().first()
        if existing:
            return None
        # Create minimal default suite
        return await self.create_suite(db, str(organization_id), current_user, name="Default", description="Auto-created")

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

    async def list_suite_id_name_map(self, db: AsyncSession, organization_id: str, current_user) -> Dict[str, str]:
        """Convenience helper returning {suite_id: suite_name} for fast lookups in UIs."""
        res = await db.execute(select(TestSuite).where(TestSuite.organization_id == str(organization_id)))
        suites = res.scalars().all()
        return {str(s.id): s.name for s in suites}

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

    # ------------------------------------------------------------------
    # YAML import / export
    # ------------------------------------------------------------------

    async def _resolve_data_source_slugs(
        self,
        db: AsyncSession,
        organization_id: str,
        slugs: List[str],
    ) -> List[str]:
        """Return ordered list of DataSource IDs for the given names."""
        if not slugs:
            return []
        res = await db.execute(
            select(DataSource).where(
                DataSource.organization_id == str(organization_id),
                DataSource.name.in_([str(s) for s in slugs]),
            )
        )
        rows = res.scalars().all()
        by_name = {ds.name: str(ds.id) for ds in rows}
        missing = [s for s in slugs if s not in by_name]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown data source slugs: {missing}",
            )
        return [by_name[s] for s in slugs]

    async def _resolve_model_slug(
        self,
        db: AsyncSession,
        organization_id: str,
        model_slug: Optional[str],
    ) -> Optional[str]:
        """Resolve ``<provider>/<model>`` to LLMModel.model_id.

        Matches provider by name or provider_type, and model by model_id or
        name. Errors if the slug is set but unresolvable.
        """
        if not model_slug:
            return None
        if "/" not in model_slug:
            raise HTTPException(
                status_code=400,
                detail=f"Model slug '{model_slug}' must be '<provider>/<model>'",
            )
        provider_part, model_part = model_slug.split("/", 1)
        res = await db.execute(
            select(LLMModel, LLMProvider)
            .join(LLMModel.provider)
            .where(LLMModel.organization_id == str(organization_id))
        )
        for model, provider in res.all():
            if provider_part not in (provider.name, provider.provider_type):
                continue
            if model_part in (model.model_id, model.name):
                return model.model_id
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model slug: {model_slug}",
        )

    async def _reverse_model_slug(
        self,
        db: AsyncSession,
        organization_id: str,
        model_id: Optional[str],
    ) -> Optional[str]:
        """Best-effort reverse of _resolve_model_slug for export."""
        if not model_id:
            return None
        res = await db.execute(
            select(LLMModel, LLMProvider)
            .join(LLMModel.provider)
            .where(
                LLMModel.organization_id == str(organization_id),
                LLMModel.model_id == model_id,
            )
            .limit(1)
        )
        row = res.first()
        if not row:
            return None
        model, provider = row
        return f"{provider.name}/{model.model_id}"

    async def _reverse_data_source_ids(
        self,
        db: AsyncSession,
        organization_id: str,
        ds_ids: Optional[List[str]],
    ) -> List[str]:
        if not ds_ids:
            return []
        res = await db.execute(
            select(DataSource).where(
                DataSource.organization_id == str(organization_id),
                DataSource.id.in_([str(i) for i in ds_ids]),
            )
        )
        rows = res.scalars().all()
        by_id = {str(r.id): r.name for r in rows}
        return [by_id[i] for i in ds_ids if i in by_id]

    async def import_yaml(
        self,
        db: AsyncSession,
        organization_id: str,
        current_user,
        yaml_text: str,
        *,
        strategy: str = "upsert",
    ) -> Dict[str, Any]:
        """Upsert a suite and its cases from YAML.

        Returns a dict with the persisted suite + a mapping of
        case-name -> case-id for convenience.
        """
        try:
            raw = yaml.safe_load(yaml_text)
        except yaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="YAML must decode to a mapping")
        try:
            suite_yaml = SuiteYaml.from_dict(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid suite YAML: {e}")

        # Suite-level data source IDs (used as default for cases without override)
        suite_ds_ids = await self._resolve_data_source_slugs(
            db, organization_id, suite_yaml.data_source_slugs
        )

        # Upsert suite by (org, name)
        res = await db.execute(
            select(TestSuite).where(
                TestSuite.organization_id == str(organization_id),
                TestSuite.name == suite_yaml.name,
            )
        )
        suite = res.scalar_one_or_none()
        if suite is None:
            suite = TestSuite(
                organization_id=str(organization_id),
                name=suite_yaml.name,
                description=suite_yaml.description,
            )
            db.add(suite)
        else:
            suite.description = suite_yaml.description
            db.add(suite)
        await db.commit()
        await db.refresh(suite)

        # Load existing cases so we can upsert by name and detect removals
        res = await db.execute(
            select(TestCase).where(TestCase.suite_id == str(suite.id))
        )
        existing_cases = {c.name: c for c in res.scalars().all()}

        seen_names: set[str] = set()
        cases_by_name: Dict[str, str] = {}

        for case_yaml in suite_yaml.cases:
            seen_names.add(case_yaml.name)
            prompt_json, additional_turns_json = await self._build_case_payload(
                db, organization_id, case_yaml, suite_ds_ids
            )
            case_ds_ids = (
                await self._resolve_data_source_slugs(
                    db, organization_id, case_yaml.data_source_slugs
                )
                if case_yaml.data_source_slugs is not None
                else suite_ds_ids
            )
            expectations_json = case_yaml.expectations.model_dump()
            case_tags = merge_tags(suite_yaml.tags, case_yaml.tags)

            tc = existing_cases.get(case_yaml.name)
            if tc is None:
                tc = TestCase(
                    suite_id=str(suite.id),
                    name=case_yaml.name,
                    prompt_json=prompt_json,
                    expectations_json=expectations_json,
                    data_source_ids_json=case_ds_ids,
                    additional_turns_json=additional_turns_json,
                    tags_json=case_tags or None,
                )
                db.add(tc)
            else:
                tc.prompt_json = prompt_json
                tc.expectations_json = expectations_json
                tc.data_source_ids_json = case_ds_ids
                tc.additional_turns_json = additional_turns_json
                tc.tags_json = case_tags or None
                # Resurrect if it was soft-deleted in a previous sync
                tc.deleted_at = None
                db.add(tc)
            await db.commit()
            await db.refresh(tc)
            cases_by_name[tc.name] = str(tc.id)

        # Handle removed cases: soft-delete on upsert (preserves TestResult
        # history), hard-delete on replace (full sync).
        removed = [
            c for name, c in existing_cases.items()
            if name not in seen_names and c.deleted_at is None
        ]
        if removed:
            now = datetime.utcnow()
            if strategy == "replace":
                for c in removed:
                    await db.delete(c)
            else:
                for c in removed:
                    c.deleted_at = now
                    db.add(c)
            await db.commit()

        return {
            "suite_id": str(suite.id),
            "suite_name": suite.name,
            "cases_by_name": cases_by_name,
            "removed_case_names": [c.name for c in removed],
        }

    async def _build_case_payload(
        self,
        db: AsyncSession,
        organization_id: str,
        case_yaml: CaseYaml,
        suite_ds_ids: List[str],
    ) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]]]:
        """Split YAML prompts/turns into the storage shape.

        Single-turn → prompt_json only, additional_turns_json=None.
        Multi-turn  → turn 1 → prompt_json; turns 2..N → additional_turns_json.
        """
        if case_yaml.is_multi_turn():
            head, *rest = case_yaml.turns or []
            head_model_id = await self._resolve_model_slug(
                db, organization_id, head.prompt.model
            )
            prompt_json = head.prompt.to_prompt_schema(
                model_id=head_model_id
            ).model_dump()
            additional: List[Dict[str, Any]] = []
            for t in rest:
                m_id = await self._resolve_model_slug(
                    db, organization_id, t.prompt.model
                )
                additional.append({
                    "prompt": t.prompt.to_prompt_schema(model_id=m_id).model_dump(),
                })
            return prompt_json, additional

        assert case_yaml.prompt is not None  # validator guarantees
        model_id = await self._resolve_model_slug(
            db, organization_id, case_yaml.prompt.model
        )
        prompt_json = case_yaml.prompt.to_prompt_schema(
            model_id=model_id
        ).model_dump()
        return prompt_json, None

    async def export_yaml(
        self,
        db: AsyncSession,
        organization_id: str,
        current_user,
        suite_id: str,
    ) -> str:
        """Serialize a suite + all cases back to YAML text."""
        suite = await self.get_suite(db, organization_id, current_user, suite_id)
        res = await db.execute(
            select(TestCase)
            .where(TestCase.suite_id == str(suite.id))
            .where(TestCase.deleted_at.is_(None))
            .order_by(TestCase.created_at.asc())
        )
        cases = res.scalars().all()

        # All data source IDs used (dedup order-preserving)
        all_ds_ids: List[str] = []
        for c in cases:
            for did in (c.data_source_ids_json or []):
                if did not in all_ds_ids:
                    all_ds_ids.append(did)
        all_ds_slugs = await self._reverse_data_source_ids(
            db, organization_id, all_ds_ids
        )

        # Tags shared by every case hoist to suite-level; per-case tags
        # carry the rest.
        per_case_tag_sets = [set(c.tags_json or []) for c in cases]
        common_tags: List[str] = []
        if per_case_tag_sets:
            # intersection of all non-empty sets — if any case has no
            # tags, there's no common set.
            if all(per_case_tag_sets):
                common = set.intersection(*per_case_tag_sets)
                # preserve order from the first case
                for t in (cases[0].tags_json or []):
                    if t in common:
                        common_tags.append(t)

        out: Dict[str, Any] = {
            "name": suite.name,
            "cases": [],
        }
        if suite.description:
            out["description"] = suite.description
        if all_ds_slugs:
            out["data_source_slugs"] = all_ds_slugs
        if common_tags:
            out["tags"] = common_tags

        for c in cases:
            case_slugs = await self._reverse_data_source_ids(
                db, organization_id, c.data_source_ids_json or []
            )
            case_dict: Dict[str, Any] = {"name": c.name}

            pj = c.prompt_json or {}
            additional = c.additional_turns_json or None

            if additional:
                turns_list: List[Dict[str, Any]] = []
                head_prompt = await self._prompt_yaml_from_json(
                    db, organization_id, pj
                )
                turns_list.append({"prompt": head_prompt})
                for t in additional:
                    tp = (t or {}).get("prompt") or {}
                    turns_list.append({
                        "prompt": await self._prompt_yaml_from_json(
                            db, organization_id, tp
                        ),
                    })
                case_dict["turns"] = turns_list
            else:
                case_dict["prompt"] = await self._prompt_yaml_from_json(
                    db, organization_id, pj
                )

            # Only include per-case data_source_slugs when they differ from
            # the suite-level list.
            if case_slugs and case_slugs != all_ds_slugs:
                case_dict["data_source_slugs"] = case_slugs

            # Case-specific tags = case tags minus the suite-level common.
            case_only_tags = [
                t for t in (c.tags_json or []) if t not in common_tags
            ]
            if case_only_tags:
                case_dict["tags"] = case_only_tags

            expectations = c.expectations_json or {"spec_version": 1, "rules": []}
            case_dict["expectations"] = expectations

            out["cases"].append(case_dict)

        return yaml.safe_dump(out, sort_keys=False)

    async def _prompt_yaml_from_json(
        self,
        db: AsyncSession,
        organization_id: str,
        prompt_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Shape a stored PromptSchema dict back into PromptYaml for export."""
        out: Dict[str, Any] = {"content": prompt_json.get("content") or ""}
        mode = prompt_json.get("mode")
        if mode and mode != "chat":
            out["mode"] = mode
        model_slug = await self._reverse_model_slug(
            db, organization_id, prompt_json.get("model_id")
        )
        if model_slug:
            out["model"] = model_slug
        return out


