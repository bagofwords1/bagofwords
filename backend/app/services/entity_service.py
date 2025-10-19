from typing import List, Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entity import Entity, entity_data_source_association
from app.models.data_source import DataSource
from app.models.user import User
from app.models.organization import Organization
from app.models.step import Step
from app.models.query import Query
from app.services.step_service import StepService
from app.services.query_service import QueryService
from app.schemas.entity_schema import EntityCreate, EntityUpdate
from datetime import datetime


class EntityService:

    def __init__(self):
        self.step_service = StepService()
        self.query_service = QueryService()


    async def create_entity_from_step(
        self,
        db: AsyncSession,
        step_id: str,
        current_user: User,
        organization: Organization,
        *,
        type_override: Optional[str] = None,
        title_override: Optional[str] = None,
        slug_override: Optional[str] = None,
        description_override: Optional[str] = None,
        publish: bool = False,
    ) -> Entity:
        """Create an Entity from a successful Step. Copies data/code as-is."""
        # Load step with query -> report (for data sources)
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.models.report import Report
        from app.models.visualization import Visualization

        result = await db.execute(
            select(Step)
            .options(selectinload(Step.query).selectinload(Query.report).selectinload(Report.data_sources))
            .where(Step.id == str(step_id))
        )
        step = result.scalar_one_or_none()
        if not step:
            raise ValueError("Step not found")
        if not step.query or not step.query.report:
            raise ValueError("Step is not linked to a query/report")
        if str(step.query.report.organization_id) != str(organization.id):
            raise ValueError("Step does not belong to this organization")
        if step.status != "success":
            raise ValueError("Only successful steps can be saved as entities")

        # Prefer a visualization for the step's query to source view
        chosen_view = None
        if getattr(step, "query_id", None):
            viz_rows = await db.execute(
                select(Visualization).where(Visualization.query_id == str(step.query_id)).order_by(Visualization.created_at.asc())
            )
            vlist = viz_rows.scalars().all()
            if vlist:
                chosen = next((v for v in vlist if getattr(v, "status", None) == "success"), vlist[0])
                chosen_view = getattr(chosen, "view", None)

        # Compute fields with overrides
        title = (title_override or step.title or "Untitled").strip()
        slug = (slug_override or step.slug or title.lower().replace(" ", "-")).strip()
        description = description_override if description_override is not None else (step.description or None)
        ent_type = (type_override or ("metric" if (chosen_view or {}).get("type") == "count" else "model"))

        entity = Entity(
            organization_id=str(organization.id),
            owner_id=str(current_user.id),
            type=ent_type,
            title=title,
            slug=slug,
            description=description,
            tags=[],
            code=step.code or "",
            data=step.data or {},
            view=(chosen_view or getattr(step, "view", None) or {"type": "table"}),
            status=("published" if publish else "draft"),
            published_at=(datetime.utcnow() if publish else None),
            last_refreshed_at=step.updated_at,
        )

        db.add(entity)
        # Link report data sources
        try:
            report_ds = list((step.query.report.data_sources or []))
            if report_ds:
                entity.data_sources = report_ds
        except Exception:
            pass

        await db.flush()
        await db.commit()
        await db.refresh(entity)
        return entity


    async def create_entity(
        self,
        db: AsyncSession,
        payload: EntityCreate,
        current_user: User,
        organization: Organization,
    ) -> Entity:
        entity = Entity(
            organization_id=str(organization.id),
            owner_id=str(current_user.id),
            type=payload.type,
            title=payload.title,
            slug=payload.slug,
            description=payload.description,
            tags=payload.tags,
            code=payload.code,
            data=payload.data,
            view=(payload.view.model_dump() if payload.view else None),
            status=payload.status,
            published_at=payload.published_at,
            last_refreshed_at=payload.last_refreshed_at,
        )
        db.add(entity)
        if payload.data_source_ids:
            result = await db.execute(select(DataSource).where(DataSource.id.in_(payload.data_source_ids)))
            entity.data_sources = list(result.scalars().all())
        await db.flush()
        await db.commit()
        await db.refresh(entity)
        return entity

    async def run_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
    ) -> Entity:
        """Execute the entity's code and update its data in place (no steps created)."""
        # Load entity and ensure org
        res = await db.execute(select(Entity).where(Entity.id == str(entity_id)))
        entity = res.scalar_one_or_none()
        if not entity:
            raise ValueError("Entity not found")
        # Optional org check if present on entity
        if getattr(entity, "organization_id", None) and str(entity.organization_id) != str(organization.id):
            raise ValueError("Entity does not belong to this organization")

        # Gather data source clients from associated data sources (if any)
        ds_clients = {}
        try:
            # Ensure data_sources is loaded minimally
            await db.refresh(entity, attribute_names=["data_sources"])  # safe with AsyncSession
            for ds in (entity.data_sources or []):
                try:
                    client = ds.get_client()
                    ds_clients[ds.name] = client
                except Exception:
                    # Best-effort: skip faulty client
                    continue
        except Exception:
            pass

        # Execute code
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        executor = StreamingCodeExecutor()
        try:
            exec_df, execution_log = executor.execute_code(
                code=entity.code or "",
                ds_clients=ds_clients,
                excel_files=[],  # per request: no excel files for now
            )
            df = executor.format_df_for_widget(exec_df)
            entity.data = df
            entity.last_refreshed_at = datetime.utcnow()
        except Exception as e:
            # Persist error info as status_reason-like in data for transparency
            try:
                entity.data = {"error": str(e)}
            except Exception:
                pass
        finally:
            db.add(entity)
            await db.commit()
            await db.refresh(entity)
        return entity

    async def list_entities(
        self,
        db: AsyncSession,
        organization: Organization,
        *,
        q: Optional[str] = None,
        type: Optional[str] = None,
        owner_id: Optional[str] = None,
        data_source_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Entity]:
        stmt = select(Entity).where(Entity.organization_id == str(organization.id))
        if type:
            stmt = stmt.where(Entity.type == type)
        if owner_id:
            stmt = stmt.where(Entity.owner_id == owner_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Entity.title.ilike(like), Entity.slug.ilike(like)))
        if data_source_id:
            stmt = stmt.join(entity_data_source_association, entity_data_source_association.c.entity_id == Entity.id)
            stmt = stmt.where(entity_data_source_association.c.data_source_id == data_source_id)
        stmt = stmt.order_by(Entity.updated_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
    ) -> Optional[Entity]:
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(Entity.id == entity_id, Entity.organization_id == str(organization.id))
        )
        return result.scalar_one_or_none()

    async def update_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        payload: EntityUpdate,
        organization: Organization,
    ) -> Optional[Entity]:
        result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            return None

        for field in ["type", "title", "slug", "description", "tags", "code", "data", "status", "published_at", "last_refreshed_at"]:
            value = getattr(payload, field, None)
            if value is not None:
                setattr(entity, field, value)
        if payload.view is not None:
            entity.view = payload.view.model_dump()
        if payload.data_source_ids is not None:
            if payload.data_source_ids:
                result = await db.execute(select(DataSource).where(DataSource.id.in_(payload.data_source_ids)))
                entity.data_sources = list(result.scalars().all())
            else:
                entity.data_sources = []

        await db.flush()
        await db.commit()
        await db.refresh(entity)
        return entity

    async def delete_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
    ) -> bool:
        result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            return False
        await db.delete(entity)
        await db.commit()
        return True


