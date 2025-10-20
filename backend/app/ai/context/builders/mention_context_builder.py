from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.mention import Mention, MentionType
from app.models.file import File
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.entity import Entity
from app.ai.context.sections.mentions_section import MentionsSection


class MentionContextBuilder:
    def __init__(self, db: AsyncSession, organization, report, head_completion):
        self.db = db
        self.organization = organization
        self.report = report
        self.head_completion = head_completion

    async def build(self, max_items_per_group: int = 10, max_columns_preview: int = 8, max_tags_preview: int = 8) -> MentionsSection:
        files: List[dict] = []
        data_sources: List[dict] = []
        tables: List[dict] = []
        entities: List[dict] = []

        if not self.head_completion:
            return MentionsSection(files=files, data_sources=data_sources, tables=tables, entities=entities)

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
                    tbl = await self.db.get(DataSourceTable, str(m.object_id))
                    # derive data source
                    ds = None
                    try:
                        ds_id = getattr(tbl, "data_source_id", None)
                        if ds_id:
                            ds = await self.db.get(DataSource, str(ds_id))
                    except Exception:
                        ds = None
                    # columns preview
                    cols_preview: List[str] = []
                    try:
                        for c in (getattr(tbl, "columns", None) or [])[:max_columns_preview]:
                            name = getattr(c, "name", None) or str(c)
                            dtype = getattr(c, "dtype", None)
                            cols_preview.append(f"{name}:{dtype}")
                        extra = max(0, len(getattr(tbl, "columns", []) or []) - len(cols_preview))
                        if extra > 0:
                            cols_preview.append(f"+{extra}")
                    except Exception:
                        pass
                    item = {
                        "id": str(m.object_id),
                        "data_source_name": getattr(ds, "name", None) if ds else None,
                        "table_name": getattr(tbl, "name", None) or m.mention_content,
                        "columns_preview": cols_preview or None,
                    }
                    tables.append(item)
                elif m.type == MentionType.ENTITY:
                    ent = await self.db.get(Entity, str(m.object_id))
                    tags = (getattr(ent, "tags", None) or [])[:max_tags_preview]
                    # Derive columns and sample from entity.data if present
                    entity_columns = None
                    entity_sample_rows = None
                    try:
                        data_json = getattr(ent, "data", None) or {}
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

        return MentionsSection(files=files, data_sources=data_sources, tables=tables, entities=entities)


