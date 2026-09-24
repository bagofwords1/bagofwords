from typing import List, Optional
from sqlalchemy import select, or_  # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, lazyload

from app.models.entity import Entity, entity_data_source_association
from app.models.organization import Organization
from app.ai.context.sections.entities_section import EntitiesSection, EntityItem


class EntityContextBuilder:
    """
    Helper for fetching catalog entities relevant to the current turn.

    Filters by organization, published status, optional entity types, and
    keyword matches across title and description. Optionally restricts to
    entities associated with the current report's data sources.
    """

    def __init__(self, db: AsyncSession, organization: Organization, report=None, user=None):
        self.db = db
        self.organization = organization
        self.report = report
        # The requesting user: entity snapshots are credential-scoped on
        # user_required/RLS sources and must be resolved per reader.
        self.user = user

    # ------------------------------------------------------------------ #
    # Keyword extraction helpers (kept local to the builder to avoid      #
    # leaking heuristics into ContextHub)                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_keywords_from_context(user_text: Optional[str], mentions_section: object | None, limit: int = 10) -> List[str]:
        source = user_text or ""

        try:
            import re as _re
            tokens = [t.lower() for t in _re.split(r"[^A-Za-z0-9_]+", source) if t]
        except Exception:
            tokens = []
        stop = {"the","a","an","and","or","to","of","in","on","for","is","are","be","it","this","that","with","by","as","at","from","have","has"}
        keywords: List[str] = []
        for tok in tokens:
            if len(tok) < 3 or tok in stop:
                continue
            if tok not in keywords:
                keywords.append(tok)
        return keywords[:limit]

    async def load_entities(
        self,
        *,
        keywords: List[str],
        types: Optional[List[str]] = None,
        top_k: int = 10,
        require_source_assoc: bool = True,
        data_source_ids: Optional[List[str]] = None,
    ) -> List[Entity]:
        stmt = (
            select(Entity)
            .options(selectinload(Entity.data_sources).options(lazyload("*")))
            .where(
                Entity.organization_id == self.organization.id,
                Entity.status == "published",
            )
        )

        if types:
            stmt = stmt.where(Entity.type.in_(types))  # type: ignore[attr-defined]

        # Restrict to the agents in play: the caller's explicit set (the run's
        # resolved agents — an Auto report has none attached), else the
        # report's attachments.
        if require_source_assoc:
            ids = data_source_ids
            if not ids:
                try:
                    ids = [str(ds.id) for ds in (getattr(self.report, "data_sources", []) or [])]
                except Exception:
                    ids = []
            if ids:
                stmt = (
                    stmt.join(
                        entity_data_source_association,
                        entity_data_source_association.c.entity_id == Entity.id,
                    )
                    .where(entity_data_source_association.c.data_source_id.in_(ids))
                )
            else:
                # No agents in play - return empty to avoid showing unrelated entities
                return []
        from app.services.bow_source_access import visible_entities_clause
        stmt = stmt.where(await visible_entities_clause(self.db, self.organization.id, self.user))
        stmt_base = stmt

        recent_first = stmt_base.order_by(
            Entity.last_refreshed_at.desc().nullslast(), Entity.updated_at.desc()
        ).limit(top_k)

        # Keyword match on title OR description (case-insensitive)
        if keywords:
            like_terms = [f"%{kw}%" for kw in keywords]
            title_clauses = [Entity.title.ilike(t) for t in like_terms]  # type: ignore[attr-defined]
            desc_clauses = [Entity.description.ilike(t) for t in like_terms]  # type: ignore[attr-defined]
            stmt = stmt.where(or_(or_(*title_clauses), or_(*desc_clauses)))
        else:
            stmt = recent_first

        res = await self.db.execute(stmt)
        rows = res.scalars().all()
        if not rows and keywords:
            # Nothing in the title/description shares a word with the ask.
            # That is the common case for a parameterized saved query ("rock
            # albums" vs "Albums by Genre") and for follow-up turns ("why not
            # the saved query?"), so fall back to the most recently refreshed
            # published entities on these agents — still bounded by top_k,
            # so the planner can at least see what exists.
            rows = (await self.db.execute(recent_first)).scalars().all()
        # De-duplicate while preserving order
        entities: List[Entity] = list(dict.fromkeys(rows))

        # Naive relevance scoring: count keyword occurrences
        if keywords:
            def score(e: Entity) -> int:
                text = f"{e.title} {(e.description or '')}".lower()
                return sum(text.count(kw.lower()) for kw in keywords)

            entities.sort(key=score, reverse=True)

        return entities[:top_k]

    async def build(
        self,
        *,
        keywords: List[str],
        types: Optional[List[str]] = None,
        top_k: int = 10,
        require_source_assoc: bool = True,
        data_source_ids: Optional[List[str]] = None,
        allow_llm_see_data: bool = True,
    ) -> EntitiesSection:
        ents = await self.load_entities(
            keywords=keywords,
            types=types,
            top_k=top_k,
            require_source_assoc=require_source_assoc,
            data_source_ids=data_source_ids,
        )
        from app.services.viewer_data_policy import resolve_entity_data
        from app.services import entity_runtime
        from app.services.entity_code import MODE_BOUND

        run_ids = list(data_source_ids or [])
        if not run_ids:
            try:
                run_ids = [str(ds.id) for ds in (getattr(self.report, "data_sources", []) or [])]
            except Exception:
                run_ids = []

        items: List[EntityItem] = []
        for e in ents:
            # A saved query shared with several agents runs on the one of them
            # in play here: show that agent, its result, and its code — never
            # another agent's name or rows, which the model would then reuse.
            target = entity_runtime.pick_target(e, None, run_ids)
            target_id = str(target.id) if target is not None else None
            try:
                if target is not None and entity_runtime.code_mode(e) != MODE_BOUND:
                    # Every agent of this conversation it is shared with, the
                    # one its code and rows below come from first: with more
                    # than one, the planner runs it on each (entities_guidance).
                    in_play = [
                        a for a in (getattr(e, "data_sources", []) or [])
                        if str(a.id) in run_ids and str(a.id) != target_id
                    ]
                    ds_names = [str(target.name or target.id)] + [str(a.name or a.id) for a in in_play]
                else:
                    ds_names = [str(getattr(ds, 'name', getattr(ds, 'id', '')) or '') for ds in (getattr(e, "data_sources", []) or [])]
                ds_names = [n for n in ds_names if n]
            except Exception:
                ds_names = []
            # Per-reader snapshot resolution: on a user-scoped source the
            # cached rows are the OWNER's slice — withheld readers get the
            # entity without data (title/description/code stay discoverable).
            data = await resolve_entity_data(self.db, e, self.user, data_source_id=target_id)
            if e.bow_source_access:
                from app.services.bow_source_access import protect_report
                await protect_report(self.db, getattr(self.report, "id", None), e.bow_source_access)
            items.append(
                EntityItem(
                    id=str(e.id),
                    type=e.type,
                    title=e.title,
                    description=e.description or "",
                    code=entity_runtime.render_for(e, target),
                    data=data or None,
                    data_model=(getattr(e, 'original_data_model', None) or getattr(e, 'view', None)),
                    ds_names=ds_names,
                    # Declared parameters: rendered so the planner passes
                    # VALUES to describe_entity(params=...) instead of
                    # writing new code for "the same query, other values".
                    parameters=list(getattr(e, 'parameters', None) or []) or None,
                )
            )
        return EntitiesSection(items=items, allow_llm_see_data=allow_llm_see_data)

    async def build_for_turn(
        self,
        *,
        types: Optional[List[str]] = None,
        top_k: int = 10,
        require_source_assoc: bool = True,
        keywords: Optional[List[str]] = None,
        user_text: Optional[str] = None,
        allow_llm_see_data: bool = True,
        data_source_ids: Optional[List[str]] = None,
    ) -> Optional[EntitiesSection]:
        # Prefer explicit keywords; else derive from current context inputs.
        # No keywords at all still builds: load_entities then lists the most
        # recent published entities on the agents in play (bounded by top_k).
        kw = [k for k in (keywords or []) if isinstance(k, str) and len(k.strip()) >= 2]
        if not kw:
            kw = self._extract_keywords_from_context(user_text, None)
        return await self.build(
            keywords=kw,
            types=types,
            top_k=top_k,
            require_source_assoc=require_source_assoc,
            data_source_ids=data_source_ids,
            allow_llm_see_data=allow_llm_see_data,
        )


