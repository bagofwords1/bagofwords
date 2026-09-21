from datetime import datetime
from typing import List

from fastapi import HTTPException
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_catalog import AgentCatalog
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.schemas.agent_catalog_schema import (
    AgentCatalogAgentsUpdate,
    AgentCatalogAssignment,
    AgentCatalogCreate,
    AgentCatalogSchema,
    AgentCatalogUpdate,
)


class AgentCatalogService:
    """CRUD for agent catalogs and their agent membership.

    Catalogs are organizational only, so nothing here touches permissions;
    the routes restrict every write to full admins.
    """

    async def list_catalogs(self, db: AsyncSession, organization: Organization) -> List[AgentCatalogSchema]:
        catalogs = (await db.execute(
            select(AgentCatalog)
            .where(
                AgentCatalog.organization_id == str(organization.id),
                AgentCatalog.deleted_at.is_(None),
            )
            .order_by(func.lower(AgentCatalog.name))
        )).scalars().all()
        counts = await self._agent_counts(db, organization, [str(c.id) for c in catalogs])
        return [self._to_schema(c, counts.get(str(c.id), 0)) for c in catalogs]

    async def create_catalog(
        self, db: AsyncSession, data: AgentCatalogCreate, organization: Organization
    ) -> AgentCatalogSchema:
        await self._assert_name_free(db, organization, data.name)
        catalog = AgentCatalog(
            organization_id=str(organization.id),
            name=data.name,
            description=data.description,
            color=data.color,
        )
        db.add(catalog)
        await db.commit()
        await db.refresh(catalog)
        return self._to_schema(catalog, 0)

    async def update_catalog(
        self, db: AsyncSession, catalog_id: str, data: AgentCatalogUpdate, organization: Organization
    ) -> AgentCatalogSchema:
        catalog = await self._get_or_404(db, catalog_id, organization)
        fields = data.model_dump(exclude_unset=True)
        if fields.get("name") is not None and fields["name"].lower() != catalog.name.lower():
            await self._assert_name_free(db, organization, fields["name"], exclude_id=str(catalog.id))
        for key in ("name", "description", "color"):
            if key in fields and not (key == "name" and fields[key] is None):
                setattr(catalog, key, fields[key])
        await db.commit()
        await db.refresh(catalog)
        counts = await self._agent_counts(db, organization, [str(catalog.id)])
        return self._to_schema(catalog, counts.get(str(catalog.id), 0))

    async def delete_catalog(
        self, db: AsyncSession, catalog_id: str, organization: Organization
    ) -> AgentCatalogSchema:
        """Soft-delete the catalog; its agents become uncatalogued (they are
        never deleted). The FK's ON DELETE SET NULL only covers hard deletes,
        so the membership is cleared explicitly here."""
        catalog = await self._get_or_404(db, catalog_id, organization)
        schema = self._to_schema(catalog, 0)
        await db.execute(
            sa_update(DataSource)
            .where(DataSource.catalog_id == str(catalog.id))
            .values(catalog_id=None)
        )
        catalog.deleted_at = datetime.utcnow()
        await db.commit()
        return schema

    async def set_catalog_agents(
        self, db: AsyncSession, catalog_id: str, data: AgentCatalogAgentsUpdate, organization: Organization
    ) -> AgentCatalogSchema:
        """Make exactly `data_source_ids` the catalog's agents.

        Listed agents move here even if they were in another catalog (one
        catalog per agent); agents currently here but not listed become
        uncatalogued.
        """
        catalog = await self._get_or_404(db, catalog_id, organization)
        wanted = {str(i) for i in data.data_source_ids}
        if wanted:
            found = set((await db.execute(
                select(DataSource.id).where(
                    DataSource.id.in_(wanted),
                    DataSource.organization_id == str(organization.id),
                    DataSource.deleted_at.is_(None),
                )
            )).scalars().all())
            missing = wanted - {str(f) for f in found}
            if missing:
                raise HTTPException(status_code=404, detail="One or more agents were not found")

        await db.execute(
            sa_update(DataSource)
            .where(
                DataSource.catalog_id == str(catalog.id),
                DataSource.id.notin_(wanted) if wanted else DataSource.id.isnot(None),
            )
            .values(catalog_id=None)
        )
        if wanted:
            await db.execute(
                sa_update(DataSource)
                .where(DataSource.id.in_(wanted))
                .values(catalog_id=str(catalog.id))
            )
        await db.commit()
        await db.refresh(catalog)
        return self._to_schema(catalog, len(wanted))

    async def set_agent_catalog(
        self, db: AsyncSession, data_source_id: str, data: AgentCatalogAssignment, organization: Organization
    ) -> AgentCatalogAssignment:
        """Put one agent in `catalog_id` (moving it out of any other catalog),
        or make it uncatalogued when `catalog_id` is None."""
        agent = (await db.execute(
            select(DataSource).where(
                DataSource.id == str(data_source_id),
                DataSource.organization_id == str(organization.id),
                DataSource.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        catalog_id = None
        if data.catalog_id:
            catalog_id = str((await self._get_or_404(db, data.catalog_id, organization)).id)
        agent.catalog_id = catalog_id
        await db.commit()
        return AgentCatalogAssignment(catalog_id=catalog_id)

    # ── helpers ──────────────────────────────────────────────────────────

    async def _get_or_404(self, db: AsyncSession, catalog_id: str, organization: Organization) -> AgentCatalog:
        catalog = (await db.execute(
            select(AgentCatalog).where(
                AgentCatalog.id == str(catalog_id),
                AgentCatalog.organization_id == str(organization.id),
                AgentCatalog.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if catalog is None:
            raise HTTPException(status_code=404, detail="Catalog not found")
        return catalog

    async def _assert_name_free(
        self, db: AsyncSession, organization: Organization, name: str, exclude_id: str | None = None
    ) -> None:
        stmt = select(AgentCatalog.id).where(
            AgentCatalog.organization_id == str(organization.id),
            AgentCatalog.deleted_at.is_(None),
            func.lower(AgentCatalog.name) == name.lower(),
        )
        if exclude_id:
            stmt = stmt.where(AgentCatalog.id != exclude_id)
        if (await db.execute(stmt)).first() is not None:
            raise HTTPException(status_code=409, detail="A catalog with this name already exists")

    async def _agent_counts(
        self, db: AsyncSession, organization: Organization, catalog_ids: List[str]
    ) -> dict:
        if not catalog_ids:
            return {}
        rows = (await db.execute(
            select(DataSource.catalog_id, func.count(DataSource.id))
            .where(
                DataSource.catalog_id.in_(catalog_ids),
                DataSource.organization_id == str(organization.id),
                DataSource.deleted_at.is_(None),
            )
            .group_by(DataSource.catalog_id)
        )).all()
        return {str(cid): int(n) for cid, n in rows}

    @staticmethod
    def _to_schema(catalog: AgentCatalog, agent_count: int) -> AgentCatalogSchema:
        return AgentCatalogSchema(
            id=str(catalog.id),
            name=catalog.name,
            description=catalog.description,
            color=catalog.color,
            agent_count=agent_count,
            created_at=catalog.created_at,
        )


agent_catalog_service = AgentCatalogService()
