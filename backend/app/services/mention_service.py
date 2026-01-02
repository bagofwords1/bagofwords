from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from typing import List, Optional, Dict, Any
from fastapi import HTTPException
from app.models.mention import Mention
from app.schemas.mention_schema import MentionCreate, MentionUpdate, MentionType
from app.models.user import User
from app.models.organization import Organization
from app.models.completion import Completion
from app.services.file_service import FileService
from app.services.data_source_service import DataSourceService
from app.services.entity_service import EntityService
from app.models.table_stats import TableStats



class MentionService:

    def __init__(self):
        self.file_service = FileService()
        self.data_source_service = DataSourceService()
        self.entity_service = EntityService()
    
    async def create_completion_mentions(self, db: AsyncSession, completion: Completion) -> Mention:

        # todo - parse mention content to extract all ids and data
        try:
            # Ensure prompt and mentions exist, default to empty list if not
            mentions = completion.prompt.get("mentions", []) if completion.prompt else []
            db_mentions = []

            # New payload structure (array of mention groups by name)
            # Example: [{ name: 'DATA SOURCES', items: [...] }, { name: 'TABLES', items: [...] }, ...]
            groups = { (g.get("name") or "").upper(): (g.get("items") or []) for g in (mentions or []) }

            # FILES
            for file_mention in groups.get("FILES", []):
                m = MentionCreate(
                    completion_id=completion.id,
                    report_id=completion.report_id,
                    type=MentionType.FILE,
                    mention_content=file_mention.get("filename") or file_mention.get("name") or "",
                    object_id=str(file_mention.get("id"))
                )
                # ensure report-file association exists
                try:
                    await self.file_service.create_or_get_report_file_association(db, completion.report_id, str(file_mention.get("id")))
                except Exception:
                    pass
                db_mentions.append(await self.create_mention(db, m, completion))

            # DATA SOURCES
            for ds_mention in groups.get("DATA SOURCES", []):
                m = MentionCreate(
                    completion_id=completion.id,
                    report_id=completion.report_id,
                    type=MentionType.DATA_SOURCE,
                    mention_content=ds_mention.get("name") or "",
                    object_id=str(ds_mention.get("id"))
                )
                db_mentions.append(await self.create_mention(db, m, completion))

            # TABLES
            for table_mention in groups.get("TABLES", []):
                m = MentionCreate(
                    completion_id=completion.id,
                    report_id=completion.report_id,
                    type=MentionType.TABLE,
                    mention_content=table_mention.get("name") or "",
                    object_id=str(table_mention.get("id"))
                )
                db_mentions.append(await self.create_mention(db, m, completion))

            # ENTITIES
            for entity_mention in groups.get("ENTITIES", []):
                m = MentionCreate(
                    completion_id=completion.id,
                    report_id=completion.report_id,
                    type=MentionType.ENTITY,
                    mention_content=entity_mention.get("title") or entity_mention.get("name") or "",
                    object_id=str(entity_mention.get("id"))
                )
                db_mentions.append(await self.create_mention(db, m, completion))
        except Exception as e:
            raise e

        return db_mentions
    
    async def create_mention(self, db: AsyncSession, mention: MentionCreate, completion: Completion) -> Mention:
        db_mention = Mention(**mention.dict())
        db.add(db_mention)
        await db.commit()
        await db.refresh(db_mention)
        return db_mention

    # =============================
    # Available mentions (autocomplete)
    # =============================
    async def get_available_mentions(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        data_source_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        all_categories = ['data_sources', 'tables', 'files', 'entities']
        requested_categories = set(categories or all_categories)

        if 'data_sources' in requested_categories:
            result['data_sources'] = await self._get_data_sources(
                db, organization, current_user, data_source_ids
            )
        else:
            result['data_sources'] = []

        if 'tables' in requested_categories:
            result['tables'] = await self._get_tables(
                db, organization, current_user, data_source_ids
            )
        else:
            result['tables'] = []

        if 'files' in requested_categories:
            result['files'] = await self._get_files(db, organization, current_user)
        else:
            result['files'] = []

        if 'entities' in requested_categories:
            result['entities'] = await self._get_entities(
                db, organization, current_user, data_source_ids
            )
        else:
            result['entities'] = []

        return result

    async def _get_data_sources(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        data_source_ids: Optional[List[str]]
    ) -> List[Dict[str, Any]]:
        data_sources = await self.data_source_service.get_active_data_sources(
            db=db,
            organization=organization,
            current_user=current_user
        )
        if data_source_ids:
            id_set = set(data_source_ids)
            data_sources = [ds for ds in data_sources if ds.id in id_set]
        items: List[Dict[str, Any]] = []
        for ds in data_sources:
            # DataSourceListItemSchema does not expose is_public explicitly
            # We set is_public=None to keep schema contract optional
            items.append({
                'id': str(ds.id),
                'type': 'data_source',
                'name': getattr(ds, 'name', ''),
                'data_source_type': getattr(ds, 'type', ''),
                'description': getattr(ds, 'description', None),
                'is_active': True,
                'is_public': None,
                'auth_policy': getattr(ds, 'auth_policy', 'system_only'),
            })
        return items

    async def _get_tables(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        data_source_ids: Optional[List[str]]
    ) -> List[Dict[str, Any]]:
        data_sources = await self.data_source_service.get_active_data_sources(
            db=db,
            organization=organization,
            current_user=current_user
        )
        if data_source_ids:
            id_set = set(data_source_ids)
            data_sources = [ds for ds in data_sources if str(getattr(ds, 'id', ds.id)) in id_set]
        tables: List[Dict[str, Any]] = []

        # Preload usage stats per (data_source_id, table_name)
        try:
            ds_ids = [str(getattr(ds, 'id', ds.id)) for ds in data_sources]
            usage_map: dict[tuple[str, str], int] = {}
            if ds_ids:
                stats_rows = await db.execute(
                    select(TableStats)
                    .where(
                        TableStats.org_id == str(organization.id),
                        TableStats.report_id == None,
                        TableStats.data_source_id.in_(ds_ids)
                    )
                )
                stats_rows = stats_rows.scalars().all()
                for s in stats_rows:
                    key = (str(getattr(s, 'data_source_id', '')), (getattr(s, 'table_fqn', '') or '').lower())
                    usage_map[key] = int(getattr(s, 'usage_count', 0) or 0)
        except Exception:
            usage_map = {}
        for ds in data_sources:
            try:
                schema = await self.data_source_service.get_data_source_schema(
                    db=db,
                    data_source_id=ds.id,
                    include_inactive=False,
                    organization=organization,
                    current_user=current_user
                )
                for table in schema:
                    columns = []
                    if hasattr(table, 'columns') and table.columns:
                        for col in table.columns:
                            columns.append({
                                'name': getattr(col, 'name', str(col)),
                                'dtype': getattr(col, 'dtype', None)
                            })
                    tables.append({
                        'id': str(table.id),
                        'type': 'datasource_table',
                        'name': table.name,
                        'datasource_id': str(ds.id),
                        'columns': columns,
                        'is_active': getattr(table, 'is_active', True),
                        'data_source_name': ds.name,
                        'data_source_type': ds.type,  # ds is DataSourceListItemSchema which has type directly
                    })
            except HTTPException:
                continue
            except Exception:
                continue
        # Order tables by usage_count desc using preloaded stats
        try:
            def usage_for(row: Dict[str, Any]) -> int:
                key = (row.get('datasource_id') or '', (row.get('name') or '').lower())
                return usage_map.get(key, 0)
            tables.sort(key=usage_for, reverse=True)
        except Exception:
            pass
        return tables

    async def _get_files(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User
    ) -> List[Dict[str, Any]]:
        try:
            files = await self.file_service.get_files(db=db, organization=organization)
            return [
                {
                    'id': str(file.id),
                    'type': 'file',
                    'filename': file.filename,
                    'content_type': file.content_type,
                    'path': file.path,
                    'created_at': file.created_at.isoformat() if getattr(file, 'created_at', None) else None,
                }
                for file in files
            ]
        except Exception:
            return []

    async def _get_entities(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        data_source_ids: Optional[List[str]]
    ) -> List[Dict[str, Any]]:
        try:
            entities = await self.entity_service.list_entities(
                db=db,
                organization=organization,
                current_user=current_user,
                skip=0,
                limit=1000,
            )
            entities = [e for e in entities if getattr(e, 'status', '') == 'published']
            # If specific data sources requested, filter entities to ANY-overlap with those ds ids
            if data_source_ids:
                id_set = set(str(x) for x in data_source_ids)
                filtered: list[Any] = []
                for e in entities:
                    try:
                        ds_ids = [str(ds.id) for ds in (getattr(e, 'data_sources', None) or [])]
                    except Exception:
                        ds_ids = []
                    if any(ds_id in id_set for ds_id in ds_ids):
                        filtered.append(e)
                entities = filtered
            # Compute mention counts for these entities
            try:
                entity_ids = [str(getattr(e, 'id', '')) for e in entities]
                mention_counts: dict[str, int] = {}
                if entity_ids:
                    counts_q = await db.execute(
                        select(Mention.object_id, func.count().label('cnt'))
                        .where(
                            Mention.type == MentionType.ENTITY,
                            Mention.object_id.in_(entity_ids)
                        )
                        .group_by(Mention.object_id)
                    )
                    for obj_id, cnt in counts_q.all():
                        mention_counts[str(obj_id)] = int(cnt or 0)
            except Exception:
                mention_counts = {}

            # Sort by mention count desc, then created_at desc
            try:
                entities.sort(
                    key=lambda e: (
                        mention_counts.get(str(getattr(e, 'id', '')), 0),
                        getattr(e, 'created_at', None) or 0
                    ),
                    reverse=True,
                )
            except Exception:
                pass

            return [
                {
                    'id': str(entity.id),
                    'type': 'entity',
                    'title': entity.title,
                    'slug': entity.slug,
                    'entity_type': entity.type,
                    'description': entity.description,
                    'status': entity.status,
                    'tags': entity.tags or [],
                    'data_source_ids': [str(ds.id) for ds in (entity.data_sources or [])],
                }
                for entity in entities
            ]
        except Exception:
            return []