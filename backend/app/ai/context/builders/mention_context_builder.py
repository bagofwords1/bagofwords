from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.mention import Mention, MentionType
from app.models.file import File
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.entity import Entity
from app.models.instruction import Instruction
from app.ai.context.sections.mentions_section import MentionsSection


class MentionContextBuilder:
    def __init__(self, db: AsyncSession, organization, report, head_completion, user=None, data_sources=None):
        self.db = db
        self.organization = organization
        self.report = report
        self.head_completion = head_completion
        # Requesting user for per-reader entity snapshot resolution.
        self.user = user
        # The agents this run is scoped to. A table mention resolves only
        # against these, under the same visibility rules as the schema
        # context (see _resolve_table_mention).
        self.data_sources = data_sources or []
        # Each agent's visible tables, keyed by agent id. Kept for this
        # builder's lifetime (one run): refresh_warm rebuilds mentions on every
        # agent loop, and one schema build per agent per run is enough.
        self._visible_cache: Dict[str, Dict[str, object]] = {}

    async def build(self, max_items_per_group: int = 10, max_columns_preview: int = 8, max_tags_preview: int = 8) -> MentionsSection:
        files: List[dict] = []
        data_sources: List[dict] = []
        tables: List[dict] = []
        entities: List[dict] = []
        instructions: List[dict] = []

        if not self.head_completion:
            return MentionsSection(files=files, data_sources=data_sources, tables=tables, entities=entities, instructions=instructions)

        # Fetch mentions for current head completion (user message of this turn)
        stmt = (
            select(Mention)
            .where(Mention.completion_id == str(self.head_completion.id))
            .order_by(Mention.created_at.asc())
        )
        res = await self.db.execute(stmt)


        rows: List[Mention] = res.scalars().all()

        for m in rows:
            try:
                if m.type == MentionType.FILE:
                    file_obj = await self.db.get(File, str(m.object_id))
                    item = {
                        "id": str(m.object_id),
                        "filename": getattr(file_obj, "filename", m.mention_content),
                        "content_type": getattr(file_obj, "content_type", None),
                        "created_at": (getattr(file_obj, "created_at", None).isoformat() if getattr(file_obj, "created_at", None) else None),
                    }
                    files.append(item)
                elif m.type == MentionType.DATA_SOURCE:
                    ds = await self.db.get(DataSource, str(m.object_id))
                    item = {
                        "id": str(m.object_id),
                        "name": getattr(ds, "name", m.mention_content),
                    }
                    data_sources.append(item)
                elif m.type == MentionType.TABLE:
                    item = await self._resolve_table_mention(m, max_columns_preview)
                    if item is not None:
                        tables.append(item)
                elif m.type == MentionType.ENTITY:
                    ent = await self.db.get(Entity, str(m.object_id))
                    tags = (getattr(ent, "tags", None) or [])[:max_tags_preview]
                    # Derive columns and sample from the POLICY-resolved data:
                    # on a user-scoped source the cached snapshot is the
                    # owner's row slice and must not leak into another
                    # reader's prompt.
                    from app.services.viewer_data_policy import resolve_entity_data
                    entity_columns = None
                    entity_sample_rows = None
                    try:
                        data_json = await resolve_entity_data(self.db, ent, self.user) if ent is not None else {}
                        # Expect optional shape: {"columns": ["col1", ...], "rows": [{...}, ...]}
                        cols = data_json.get("columns") if isinstance(data_json, dict) else None
                        rows = data_json.get("rows") if isinstance(data_json, dict) else None
                        if isinstance(cols, list):
                            entity_columns = [str(c) for c in cols][:max_columns_preview]
                        if isinstance(rows, list):
                            entity_sample_rows = rows[:2]
                    except Exception:
                        pass
                    item = {
                        "id": str(m.object_id),
                        "title": getattr(ent, "title", None) or m.mention_content,
                        "entity_type": getattr(ent, "type", None),
                        "status": getattr(ent, "status", None),
                        "description": getattr(ent, "description", None),
                        "code": getattr(ent, "code", None),
                        "columns": entity_columns,
                        "sample_rows": entity_sample_rows,
                    }
                    entities.append(item)
                elif m.type == MentionType.INSTRUCTION:
                    # Force-include the mentioned instruction/skill's full content
                    # regardless of load_mode / agent scoping (mirrors FILE).
                    ins = await self.db.get(Instruction, str(m.object_id))
                    item = {
                        "id": str(m.object_id),
                        "title": getattr(ins, "title", None) or m.mention_content,
                        "kind": getattr(ins, "kind", None) or "instruction",
                        "text": getattr(ins, "text", None) or "",
                    }
                    instructions.append(item)
            except Exception:
                # Best-effort; skip broken items
                continue

        # Truncate to max_items_per_group
        if len(files) > max_items_per_group:
            files = files[:max_items_per_group]
        if len(data_sources) > max_items_per_group:
            data_sources = data_sources[:max_items_per_group]
        if len(tables) > max_items_per_group:
            tables = tables[:max_items_per_group]
        if len(entities) > max_items_per_group:
            entities = entities[:max_items_per_group]
        if len(instructions) > max_items_per_group:
            instructions = instructions[:max_items_per_group]

        return MentionsSection(files=files, data_sources=data_sources, tables=tables, entities=entities, instructions=instructions)

    async def _resolve_table_mention(self, m: Mention, max_columns_preview: int) -> Optional[dict]:
        """Render a mentioned table only if this run's schema context shows it.

        The mention's object_id comes from the client, so it is not proof the
        caller may see the table. Resolving through SchemaContextBuilder applies
        the same rules as the rest of the prompt: the table belongs to one of
        this run's agents, is activated on it, and, on a delegated connection,
        is reachable with the caller's own credentials, listing only the
        columns they can reach. Anything else is dropped rather than rendered
        from the raw catalog row.
        """
        tbl = await self.db.get(DataSourceTable, str(m.object_id))
        if tbl is None:
            return None
        ds = next(
            (d for d in self.data_sources if str(getattr(d, "id", "")) == str(tbl.datasource_id)),
            None,
        )
        if ds is None:
            return None

        visible = await self._visible_tables(ds)
        prompt_table = visible.get(str(tbl.id))
        if prompt_table is None:
            return None

        cols = list(getattr(prompt_table, "columns", None) or [])
        cols_preview: List[str] = [f"{c.name}:{c.dtype}" for c in cols[:max_columns_preview]]
        extra = len(cols) - len(cols_preview)
        if extra > 0:
            cols_preview.append(f"+{extra}")
        return {
            "id": str(m.object_id),
            "data_source_name": getattr(ds, "name", None),
            "table_name": prompt_table.name,
            "columns_preview": cols_preview or None,
        }

    async def _visible_tables(self, ds) -> Dict[str, object]:
        """Tables of `ds` the schema context shows this user, keyed by
        canonical DataSourceTable id. Built once per agent per run."""
        cache = self._visible_cache
        key = str(ds.id)
        if key not in cache:
            from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
            builder = SchemaContextBuilder(self.db, [ds], self.organization, self.report, user=self.user)
            ctx = await builder.build(
                with_stats=False,
                data_source_ids=[key],
                active_only=True,
                split_file_scopes=False,
            )
            cache[key] = {
                str(t.id): t
                for section in ctx.data_sources
                for t in (section.tables or [])
                if getattr(t, "id", None)
            }
        return cache[key]
