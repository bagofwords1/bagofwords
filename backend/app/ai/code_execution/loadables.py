"""
Loadables: let generated `generate_df` code reuse data the caller already has.

Exposes two callables to the sandbox — `load_step(id_or_name)` and
`load_entity(id_or_name)` — each returning a pandas DataFrame.

Because generated code runs in a worker thread with no event loop and the
sandbox forbids I/O, resolution never happens lazily inside the sandbox.
Instead the pipeline:

  1. AST-scans the generated code for `load_step(...)` / `load_entity(...)`
     calls with string-literal arguments (`extract_loadable_refs`),
  2. resolves just those refs to DataFrames here (async, with access checks),
  3. hands the in-memory registry to the executor, which builds pure-lookup
     closures over it.

Scope:
  - load_step   -> the current report's default steps (Report -> Query -> Step),
                   successful only. Widget is not consulted (deprecated).
  - load_entity -> published catalog entities whose data sources the caller
                   may access (user_can_access_data_source).
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entity import Entity
from app.models.query import Query
from app.models.step import Step
from app.ai.context.sections.steps_section import StepItem, StepsSection


# Names recognised in generated code.
_STEP_FN = "load_step"
_ENTITY_FN = "load_entity"


# Discovery-only recency window for load_step (seconds). Deliberately a fixed
# internal default rather than an org setting — it keeps the settings UI to a
# single load_step toggle instead of an orphaned knob that only matters when the
# toggle is on. Bounds what discovery advertises; resolution is never bounded by
# it, so re-running saved code that references an older step keeps working.
STEP_DISCOVERY_MAX_AGE_SECONDS = 300


def load_step_settings(organization_settings) -> Tuple[bool, Optional[int]]:
    """Return ``(enabled, max_age_seconds)`` for load_step.

    ``enabled`` comes from the org's ``enable_load_step`` setting (default off).
    ``max_age_seconds`` is the fixed internal discovery window
    (STEP_DISCOVERY_MAX_AGE_SECONDS) — not a user-facing setting.
    """
    if organization_settings is None:
        return False, STEP_DISCOVERY_MAX_AGE_SECONDS
    try:
        cfg = organization_settings.get_config("enable_load_step")
        enabled = bool(getattr(cfg, "value", False))
    except Exception:
        enabled = False
    return enabled, STEP_DISCOVERY_MAX_AGE_SECONDS


def extract_loadable_refs(code: str) -> Tuple[List[str], List[str]]:
    """Return (step_refs, entity_refs) from string-literal calls in `code`.

    Only literal string arguments are extracted — `load_step(some_var)` is
    ignored (it cannot be pre-resolved). Duplicates are de-duplicated while
    preserving first-seen order.
    """
    step_refs: List[str] = []
    entity_refs: List[str] = []
    if not code:
        return step_refs, entity_refs
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return step_refs, entity_refs

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        fn = node.func.id
        if fn not in (_STEP_FN, _ENTITY_FN) or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            ref = first.value
            bucket = step_refs if fn == _STEP_FN else entity_refs
            if ref not in bucket:
                bucket.append(ref)
    return step_refs, entity_refs


def grid_to_df(data) -> pd.DataFrame:
    """Reconstruct a DataFrame from a stored `{rows, columns}` grid.

    Column order follows `columns[*].field` when present; any unexpected
    columns are appended after the known ones.
    """
    if not isinstance(data, dict):
        return pd.DataFrame()
    rows = data.get("rows") or []
    df = pd.DataFrame(rows)
    cols = data.get("columns") or []
    fields = [c.get("field") for c in cols if isinstance(c, dict) and c.get("field")]
    if fields and not df.empty:
        present = [f for f in fields if f in df.columns]
        if present:
            extra = [c for c in df.columns if c not in present]
            df = df[present + extra]
    return df


async def resolve_loadables_for_code(
    db: AsyncSession, organization, report, current_user, code: str,
    *, enable_load_step: bool = True,
) -> Optional[Dict]:
    """One-shot helper for non-streaming paths (step rerun / query run).

    AST-scans `code`, resolves any load_step/load_entity refs, and returns the
    `{"steps": {...}, "entities": {...}}` registry — or None when the code uses
    neither. Resolution errors are left for the in-sandbox closures to raise at
    call time (surfacing as the step's error), keeping this helper drop-in.

    `enable_load_step` gates the step half only; when off, `load_step` refs in
    saved code are not resolved (they surface as the sandbox closure's error).
    """
    step_refs, entity_refs = extract_loadable_refs(code or "")
    if not step_refs and not entity_refs:
        return None
    resolver = LoadablesResolver(
        db, organization, report, current_user, enable_load_step=enable_load_step
    )
    resolved = await resolver.resolve(step_refs, entity_refs)
    return {"steps": resolved.get("steps", {}), "entities": resolved.get("entities", {})}


class LoadablesResolver:
    """Resolves load_step / load_entity references for a single report turn."""

    def __init__(
        self, db: AsyncSession, organization, report=None, current_user=None,
        *, enable_load_step: bool = True, step_max_age_seconds: Optional[int] = None,
    ):
        self.db = db
        self.organization = organization
        self.report = report
        self.current_user = current_user
        # When False, load_step is disabled: no steps are advertised for
        # discovery and none are resolved. load_entity is unaffected.
        self.enable_load_step = enable_load_step
        # Discovery-only recency bound (seconds). None/<=0 means no bound.
        # Resolution is deliberately never bounded by this, so re-running saved
        # code that references an older step keeps working.
        self.step_max_age_seconds = step_max_age_seconds

    # ------------------------------------------------------------------ #
    # Discovery (bounded) — what the prompt advertises as loadable.        #
    # ------------------------------------------------------------------ #
    async def list_for_discovery(self, *, limit: int = 25) -> Optional[StepsSection]:
        """Return a bounded `<available_steps>` section for the prompt.

        Lists the current report's successful default steps (most recent
        first, within the recency window). Resolution is not limited to this
        list — `load_step` can load any default step in the report by id or
        name. Returns None when load_step is disabled.
        """
        if not self.enable_load_step:
            return None
        steps = await self._report_default_steps(
            limit=limit, max_age_seconds=self.step_max_age_seconds
        )
        if not steps:
            return None
        items: List[StepItem] = []
        for step in steps:
            data = step.data if isinstance(step.data, dict) else {}
            columns = [
                c.get("field")
                for c in (data.get("columns") or [])
                if isinstance(c, dict) and c.get("field")
            ]
            info = data.get("info") or {}
            row_count = info.get("total_rows")
            if row_count is None:
                row_count = len(data.get("rows") or [])
            items.append(
                StepItem(
                    id=str(step.id),
                    title=step.title or "",
                    slug=step.slug,
                    row_count=row_count,
                    columns=columns,
                )
            )
        return StepsSection(items=items)

    # ------------------------------------------------------------------ #
    # Resolution (indexed) — the refs the generated code actually names.   #
    # ------------------------------------------------------------------ #
    async def resolve(self, step_refs: List[str], entity_refs: List[str]) -> Dict:
        """Resolve refs to DataFrames. Never raises; misses go in `errors`."""
        result: Dict = {"steps": {}, "entities": {}, "errors": []}

        # Resolution is never bounded by the discovery recency window — a
        # reference in already-generated/saved code must resolve regardless of
        # age. The enable flag, however, disables the step half entirely.
        if step_refs and self.enable_load_step:
            steps = await self._report_default_steps()
            by_id: Dict[str, Step] = {}
            by_slug: Dict[str, Step] = {}
            by_title: Dict[str, Step] = {}
            for step in steps:
                by_id[str(step.id)] = step
                if step.slug:
                    by_slug[step.slug.lower()] = step
                title = (step.title or "").lower()
                # On title collision the most recent step wins.
                if title and (
                    title not in by_title
                    or (step.created_at or 0) > (by_title[title].created_at or 0)
                ):
                    by_title[title] = step

            for ref in step_refs:
                key = str(ref)
                step = (
                    by_id.get(key)
                    or by_slug.get(key.lower())
                    or by_title.get(key.lower())
                )
                if step is None:
                    result["errors"].append(
                        f"load_step({key!r}): no matching step in this report. "
                        f"Available steps: {sorted({s.title for s in steps if s.title})}"
                    )
                    continue
                result["steps"][key] = grid_to_df(step.data)

        for ref in entity_refs or []:
            key = str(ref)
            entity, err = await self._resolve_entity(key)
            if entity is None:
                result["errors"].append(err)
                continue
            result["entities"][key] = grid_to_df(entity.data)

        return result

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #
    async def _report_default_steps(
        self, *, limit: Optional[int] = None, max_age_seconds: Optional[int] = None,
    ) -> List[Step]:
        """Successful default steps for the report (Report -> Query -> Step).

        `max_age_seconds` (>0) bounds by `Step.created_at` recency — used by
        discovery only. Resolution passes it as None so any step remains
        loadable by id/name regardless of age.
        """
        if self.report is None:
            return []
        stmt = (
            select(Step)
            .join(Query, Step.query_id == Query.id)
            .where(
                Query.report_id == str(self.report.id),
                Step.id == Query.default_step_id,
                Step.status == "success",
            )
            .order_by(Step.created_at.desc())
        )
        if max_age_seconds and max_age_seconds > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
            # Step.created_at is stored naive-UTC; compare against a naive cutoff
            # to avoid tz-aware/naive mismatches across SQLite/Postgres.
            stmt = stmt.where(Step.created_at >= cutoff.replace(tzinfo=None))
        if limit:
            stmt = stmt.limit(limit)
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def _resolve_entity(self, ref: str) -> Tuple[Optional[Entity], str]:
        """Find a published entity by id/slug/title/fuzzy, then access-check it."""
        org_id = str(self.organization.id)

        async def _q(*where):
            res = await self.db.execute(
                select(Entity)
                .options(selectinload(Entity.data_sources))
                .where(
                    Entity.organization_id == org_id,
                    Entity.status == "published",
                    Entity.deleted_at == None,  # noqa: E711
                    *where,
                )
                .limit(1)
            )
            return res.scalar_one_or_none()

        entity = (
            await _q(Entity.id == ref)
            or await _q(Entity.slug.ilike(ref))
            or await _q(Entity.title.ilike(ref))
            or await _q(
                or_(
                    Entity.title.ilike(f"%{ref}%"),
                    Entity.slug.ilike(f"%{ref}%"),
                    Entity.description.ilike(f"%{ref}%"),
                )
            )
        )
        if entity is None:
            return None, f"load_entity({ref!r}): no matching published entity found."

        if entity.data_sources and self.current_user is not None:
            from app.core.permission_resolver import user_can_access_data_source

            for ds in entity.data_sources:
                if not await user_can_access_data_source(
                    self.db, str(self.current_user.id), org_id, ds
                ):
                    return None, (
                        f"load_entity({ref!r}): you do not have access to its "
                        f"data source '{getattr(ds, 'name', ds.id)}'."
                    )
        return entity, ""
