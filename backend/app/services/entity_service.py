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
        data_source_ids_override: Optional[List[str]] = None,
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

        # Check if user is admin (has create_entities permission)
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        is_admin = self._is_admin_permissions(user_permissions)

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
            last_refreshed_at=step.updated_at,
            source_step_id=str(step_id),  # Link back to source step
        )

        # Apply dual-status workflow based on user role
        if is_admin:
            # Admin can create global entities - respect their publish choice
            entity.private_status = None
            entity.global_status = "approved"
            entity.status = "published" if publish else "draft"
            entity.published_at = datetime.utcnow() if publish else None
        else:
            # Regular users create suggested entities (pending admin approval)
            entity.private_status = "published"
            entity.global_status = "suggested"
            entity.status = "draft"
            entity.published_at = None

        db.add(entity)
        # Link data sources - use override if provided, otherwise use report data sources
        try:
            if data_source_ids_override is not None and len(data_source_ids_override) > 0:
                # Use explicitly provided data source IDs
                result = await db.execute(select(DataSource).where(DataSource.id.in_(data_source_ids_override)))
                entity.data_sources = list(result.scalars().all())
            else:
                # Fall back to report data sources
                report_ds = list((step.query.report.data_sources or []))
                if report_ds:
                    entity.data_sources = report_ds
        except Exception:
            pass

        await db.flush()
        await db.commit()
        await db.refresh(entity)
        
        # Bidirectional relationship is automatically maintained by SQLAlchemy
        # through entity.source_step_id - no need to manually set step.created_entity_id
        
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

    async def list_entities(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        *,
        q: Optional[str] = None,
        type: Optional[str] = None,
        owner_id: Optional[str] = None,
        data_source_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Entity]:
        # Get user's accessible data sources
        from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
        from sqlalchemy import exists, and_
        
        accessible_ds_subquery = (
            select(DataSource.id)
            .filter(DataSource.organization_id == organization.id)
            .filter(
                or_(
                    DataSource.is_public == True,  # Public data sources
                    DataSource.id.in_(
                        select(DataSourceMembership.data_source_id)
                        .filter(
                            DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                            DataSourceMembership.principal_id == current_user.id
                        )
                    )  # User has explicit membership
                )
            )
        )
        
        # Subquery to check if entity has any inaccessible data sources
        has_inaccessible_ds = exists(
            select(1)
            .select_from(entity_data_source_association)
            .where(
                and_(
                    entity_data_source_association.c.entity_id == Entity.id,
                    entity_data_source_association.c.data_source_id.notin_(accessible_ds_subquery)
                )
            )
        )
        
        # Base query: show entities where user has access to ALL data sources
        # (i.e., entities that don't have any inaccessible data sources)
        stmt = (
            select(Entity)
            .where(Entity.organization_id == str(organization.id))
            .where(Entity.deleted_at == None)
            .where(~has_inaccessible_ds)  # Exclude entities with any inaccessible data sources
        )
        
        if type:
            stmt = stmt.where(Entity.type == type)
        if owner_id:
            stmt = stmt.where(Entity.owner_id == owner_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Entity.title.ilike(like), Entity.slug.ilike(like)))
        if data_source_id:
            # Filter to entities that have this specific data source
            stmt = stmt.where(
                exists(
                    select(1)
                    .select_from(entity_data_source_association)
                    .where(
                        and_(
                            entity_data_source_association.c.entity_id == Entity.id,
                            entity_data_source_association.c.data_source_id == data_source_id
                        )
                    )
                )
            )
        
        stmt = stmt.order_by(Entity.updated_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
        current_user: Optional[User] = None,
    ) -> Optional[Entity]:
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(Entity.id == entity_id, Entity.organization_id == str(organization.id))
        )
        entity = result.scalar_one_or_none()
        
        if not entity or not current_user:
            return entity
        
        # Check if user has access to all data sources of this entity
        if entity.data_sources:
            from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
            
            for ds in entity.data_sources:
                # Check if data source is public or user has membership
                if not ds.is_public:
                    has_membership = await db.execute(
                        select(DataSourceMembership)
                        .where(
                            DataSourceMembership.data_source_id == ds.id,
                            DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                            DataSourceMembership.principal_id == current_user.id
                        )
                    )
                    if not has_membership.scalar_one_or_none():
                        # User doesn't have access to this data source
                        return None
        
        return entity

    async def update_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        payload: EntityUpdate,
        organization: Organization,
        current_user: User,
    ) -> Optional[Entity]:
        result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            return None

        # Get user permissions
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        from fastapi import HTTPException
        if not user_permissions:
            raise HTTPException(status_code=403, detail="Permission denied: not an organization member")
        
        # Determine what type of update this is and check permissions
        from app.models.entity import Entity as EntityModel
        update_type = self._determine_update_type(entity, payload, current_user, user_permissions)
        
        # Handle the update based on type
        if update_type == "admin_review":
            await self._handle_admin_review(entity, payload, current_user)
        elif update_type == "admin_edit":
            await self._handle_admin_edit(entity, payload, current_user)
        elif update_type == "owner_edit":
            await self._handle_owner_edit(entity, payload)
        else:
            raise HTTPException(status_code=403, detail="Permission denied")

        # Handle data source associations
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

    def _is_admin_permissions(self, user_permissions: set) -> bool:
        """Check if permissions set corresponds to an admin in org"""
        return 'update_entities' in user_permissions or 'create_entities' in user_permissions or 'delete_entities' in user_permissions

    async def _get_user_permissions(self, db: AsyncSession, user: User, organization: Organization) -> set:
        """Get user's permissions in the organization"""
        from app.models.membership import Membership, ROLES_PERMISSIONS
        
        stmt = select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization.id
        )
        result = await db.execute(stmt)
        membership = result.scalar_one_or_none()
        
        return ROLES_PERMISSIONS.get(membership.role, set()) if membership else set()

    def _determine_update_type(self, entity: Entity, payload: EntityUpdate, current_user: User, user_permissions: set) -> str:
        """Determine what type of update this is based on permissions and changes"""
        is_admin = self._is_admin_permissions(user_permissions)
        is_owner = entity.owner_id == current_user.id
        is_suggested = entity.global_status == "suggested"
        has_status_change = payload.status and payload.status != entity.status
        
        # Admin reviewing a suggested entity (approve or reject)
        if is_admin and is_suggested and has_status_change:
            if payload.status in ["published", "archived"]:
                return "admin_review"
        
        # Admin editing any entity (not review)
        elif is_admin:
            return "admin_edit"
        
        # Owner editing their own entity when not globally approved (suggested or private)
        elif is_owner and (entity.global_status != "approved") and user_permissions:
            return "owner_edit"
        
        # No permission
        else:
            return "no_permission"

    async def _handle_admin_review(self, entity: Entity, payload: EntityUpdate, admin_user: User):
        """Handle admin reviewing a suggested entity (approve or reject)"""
        if payload.status == "published":
            # APPROVAL: Suggested -> Global Published
            # From: published, suggested, draft
            # To: null, approved, published
            entity.private_status = None
            entity.global_status = "approved"
            entity.status = "published"
            entity.reviewed_by_user_id = admin_user.id
            
        elif payload.status == "archived":
            # REJECTION: Suggested -> Private Archived
            # From: published, suggested, draft
            # To: published, rejected, archived
            entity.private_status = "published"
            entity.global_status = "rejected"
            entity.status = "archived"
            entity.reviewed_by_user_id = admin_user.id
        
        # Apply other changes from the form
        allowed_fields = ['title', 'description', 'type', 'code', 'tags']
        for field in allowed_fields:
            if hasattr(payload, field) and getattr(payload, field) is not None:
                setattr(entity, field, getattr(payload, field))
        
        if payload.view is not None:
            entity.view = payload.view.model_dump()

    async def _handle_admin_edit(self, entity: Entity, payload: EntityUpdate, admin_user: User):
        """Handle admin editing any entity (not review)"""
        # Admin can change status and gets credited as reviewer for status changes
        if payload.status and payload.status != entity.status:
            if payload.status in ["published", "archived"]:
                entity.reviewed_by_user_id = admin_user.id
        
        # Apply all changes (admin has full control)
        update_data = payload.model_dump(exclude_unset=True, exclude={'data_source_ids'})
        for field, value in update_data.items():
            if field == 'view' and value is not None:
                entity.view = value.model_dump() if hasattr(value, 'model_dump') else value
            else:
                setattr(entity, field, value)

    async def _handle_owner_edit(self, entity: Entity, payload: EntityUpdate):
        """Handle owner editing their own private entity"""
        # Owner can edit most fields except status changes
        allowed_fields = ['title', 'description', 'type', 'code', 'tags', 'data']
        
        for field in allowed_fields:
            if hasattr(payload, field) and getattr(payload, field) is not None:
                setattr(entity, field, getattr(payload, field))
        
        if payload.view is not None:
            entity.view = payload.view.model_dump() if hasattr(payload.view, 'model_dump') else payload.view


