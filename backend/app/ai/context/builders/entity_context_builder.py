from typing import List, Optional
from sqlalchemy import select, or_  # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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

    def __init__(self, db: AsyncSession, organization: Organization, report=None):
        self.db = db
        self.organization = organization
        self.report = report

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
            .options(selectinload(Entity.data_sources))
            .where(
                Entity.organization_id == self.organization.id,
                Entity.status == "published",
            )
        )

        if types:
            stmt = stmt.where(Entity.type.in_(types))  # type: ignore[attr-defined]

        # Keyword match on title OR description (case-insensitive)
        if keywords:
            like_terms = [f"%{kw}%" for kw in keywords]
            title_clauses = [Entity.title.ilike(t) for t in like_terms]  # type: ignore[attr-defined]
            desc_clauses = [Entity.description.ilike(t) for t in like_terms]  # type: ignore[attr-defined]
            stmt = stmt.where(or_(or_(*title_clauses), or_(*desc_clauses)))

        # Restrict to data sources associated to this report
        if require_source_assoc:
            ids = data_source_ids
            if ids is None:
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
                # No data sources on report - return empty to avoid showing unrelated entities
                return []

        res = await self.db.execute(stmt)
        rows = res.scalars().all()
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
        items: List[EntityItem] = []
        for e in ents:
            try:
                ds_names = [str(getattr(ds, 'name', getattr(ds, 'id', '')) or '') for ds in (getattr(e, "data_sources", []) or [])]
                ds_names = [n for n in ds_names if n]
            except Exception:
                ds_names = []
            items.append(
                EntityItem(
                    id=str(e.id),
                    type=e.type,
                    title=e.title,
                    description=e.description or "",
                    code=getattr(e, 'code', None),
                    data=getattr(e, 'data', None),
                    data_model=(getattr(e, 'original_data_model', None) or getattr(e, 'view', None)),
                    ds_names=ds_names,
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
    ) -> Optional[EntitiesSection]:
        # Prefer explicit keywords; else derive from current context inputs
        kw = [k for k in (keywords or []) if isinstance(k, str) and len(k.strip()) >= 2]
        if not kw:
            kw = self._extract_keywords_from_context(user_text, None)
        if not kw:
            return None
        return await self.build(
            keywords=kw,
            types=types,
            top_k=top_k,
            require_source_assoc=require_source_assoc,
            allow_llm_see_data=allow_llm_see_data,
        )


