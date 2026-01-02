"""
Demo Data Source Service

Handles listing and installing demo data sources.
"""

from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.models.data_source import DataSource
from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
from app.models.user import User
from app.models.organization import Organization
from app.schemas.demo_data_source_schema import (
    DEMO_DATA_SOURCES,
    DemoDataSourceDefinition,
    DemoDataSourceListItem,
    DemoDataSourceInstallResponse,
    get_demo_data_source,
    list_demo_data_sources as list_demo_definitions,
)

import json
import logging

from app.core.telemetry import telemetry

logger = logging.getLogger(__name__)


# Marker key stored in config to identify demo data sources
DEMO_ID_KEY = "demo_id"


class DemoDataSourceService:

    async def list_demo_data_sources(
        self,
        db: AsyncSession,
        organization: Organization,
    ) -> List[DemoDataSourceListItem]:
        """
        List all available demo data sources with their installation status.
        """
        # Get all demo definitions
        demos = list_demo_definitions()

        # Check which ones are already installed for this organization
        installed_demos = await self._get_installed_demos(db, organization.id)

        result = []
        for demo in demos:
            installed_ds = installed_demos.get(demo.id)
            result.append(
                DemoDataSourceListItem(
                    id=demo.id,
                    name=demo.name,
                    description=demo.description,
                    type=demo.type,
                    installed=installed_ds is not None,
                    installed_data_source_id=installed_ds.id if installed_ds else None,
                )
            )

        return result

    async def install_demo_data_source(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        demo_id: str,
    ) -> DemoDataSourceInstallResponse:
        """
        Install a demo data source for the organization.
        
        If already installed, returns the existing data source.
        """
        # Get demo definition
        demo = get_demo_data_source(demo_id)
        if not demo:
            return DemoDataSourceInstallResponse(
                success=False,
                message=f"Demo data source '{demo_id}' not found",
            )

        # Check if already installed
        existing = await self._get_installed_demo(db, organization.id, demo_id)
        if existing:
            return DemoDataSourceInstallResponse(
                success=True,
                message=f"Demo data source '{demo.name}' is already installed",
                data_source_id=existing.id,
                already_installed=True,
            )

        # Create the data source
        try:
            data_source = await self._create_demo_data_source(
                db=db,
                organization=organization,
                current_user=current_user,
                demo=demo,
            )

            # Telemetry: track demo installation
            try:
                await telemetry.capture(
                    "demo_data_source_installed",
                    {
                        "demo_id": demo_id,
                        "demo_name": demo.name,
                        "demo_type": demo.type,
                        "data_source_id": str(data_source.id),
                        "num_instructions": len(demo.instructions),
                        "num_conversation_starters": len(demo.conversation_starters),
                    },
                    user_id=current_user.id,
                    org_id=organization.id,
                )
            except Exception:
                pass  # Never fail the request due to telemetry

            return DemoDataSourceInstallResponse(
                success=True,
                message=f"Successfully installed '{demo.name}'",
                data_source_id=data_source.id,
                already_installed=False,
            )

        except Exception as e:
            logger.error(f"Failed to install demo data source {demo_id}: {e}")
            return DemoDataSourceInstallResponse(
                success=False,
                message=f"Failed to install demo data source: {str(e)}",
            )

    async def _create_demo_data_source(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        demo: DemoDataSourceDefinition,
    ) -> DataSource:
        """
        Create a data source from a demo definition.
        
        This bypasses the normal registry dev_only check since demos
        are explicitly whitelisted.
        """
        # Build config with demo_id marker
        config = demo.config.copy()
        config[DEMO_ID_KEY] = demo.id

        # Create the Connection first
        from app.models.connection import Connection
        connection = Connection(
            name=demo.name,
            type=demo.type,
            config=json.dumps(config),
            organization_id=str(organization.id),
            is_active=True,
            auth_policy="system_only",
        )
        
        # Encrypt credentials on connection (even if empty, for consistency)
        if demo.credentials:
            connection.encrypt_credentials(demo.credentials)
        
        db.add(connection)
        await db.flush()

        # Create the data source (Domain)
        data_source = DataSource(
            name=demo.name,
            organization_id=organization.id,
            is_public=True,
            is_active=True,  # Explicitly set active
            use_llm_sync=False,
            owner_user_id=current_user.id,
            description=demo.description,
            conversation_starters=demo.conversation_starters if demo.conversation_starters else None,
        )

        db.add(data_source)
        await db.flush()
        
        # Associate data source with connection via junction table
        from app.models.domain_connection import domain_connection
        await db.execute(
            domain_connection.insert().values(
                data_source_id=data_source.id,
                connection_id=connection.id
            )
        )
        
        await db.commit()
        
        # Reload with connections eagerly loaded
        from sqlalchemy.orm import selectinload
        result = await db.execute(
            select(DataSource)
            .where(DataSource.id == data_source.id)
            .options(selectinload(DataSource.connections))
        )
        data_source = result.scalar_one()

        logger.info(
            f"Created demo data source: {demo.name} (id={data_source.id}) "
            f"for org={organization.id}"
        )

        # Add the creator as a member
        membership = DataSourceMembership(
            data_source_id=data_source.id,
            principal_type=PRINCIPAL_TYPE_USER,
            principal_id=current_user.id,
        )
        db.add(membership)
        await db.commit()

        # Load and save tables from the data source
        await self._load_tables(db, data_source, organization, current_user)

        # Test connection to set proper status
        await self._test_connection(db, data_source, organization, current_user)

        # Create instructions for this data source
        await self._create_instructions(db, data_source, organization, current_user, demo)

        return data_source

    async def _test_connection(
        self,
        db: AsyncSession,
        data_source: DataSource,
        organization: Organization,
        current_user: User,
    ):
        """
        Test the connection to ensure proper status is set.
        """
        from app.services.data_source_service import DataSourceService
        
        try:
            ds_service = DataSourceService()
            result = await ds_service.test_data_source_connection(
                db=db,
                data_source_id=data_source.id,
                organization=organization,
                current_user=current_user,
            )
            logger.info(f"Connection test for demo data source {data_source.name}: {result}")
        except Exception as e:
            logger.warning(f"Failed to test connection for demo data source {data_source.name}: {e}")

    async def _create_instructions(
        self,
        db: AsyncSession,
        data_source: DataSource,
        organization: Organization,
        current_user: User,
        demo: DemoDataSourceDefinition,
    ):
        """
        Create instructions for the demo data source.
        """
        if not demo.instructions:
            return

        from app.services.instruction_service import InstructionService
        from app.schemas.instruction_schema import InstructionCreate

        try:
            instruction_service = InstructionService()
            
            # === Build System Integration ===
            # Create a single build for all demo instructions
            demo_build = None
            try:
                from app.services.build_service import BuildService
                build_service = BuildService()
                demo_build = await build_service.get_or_create_draft_build(
                    db,
                    organization.id,
                    source='user',
                    user_id=current_user.id
                )
                logger.debug(f"Created demo build {demo_build.id} for data source {data_source.name}")
            except Exception as build_error:
                logger.warning(f"Failed to create demo build: {build_error}")
            
            created_count = 0
            for instruction_text in demo.instructions:
                instruction_data = InstructionCreate(
                    text=instruction_text,
                    data_source_ids=[data_source.id],
                    category="general",
                )
                
                await instruction_service.create_instruction(
                    db=db,
                    instruction_data=instruction_data,
                    current_user=current_user,
                    organization=organization,
                    force_global=True,  # Make them global/approved so they're active immediately
                    build=demo_build,  # Use shared build
                    auto_finalize=False,  # Don't finalize yet
                )
                created_count += 1
            
            # === Finalize Build ===
            if demo_build and created_count > 0:
                try:
                    await build_service.submit_build(db, demo_build.id)
                    await build_service.approve_build(db, demo_build.id, approved_by_user_id=current_user.id)
                    await build_service.promote_build(db, demo_build.id)
                    logger.info(f"Finalized demo build {demo_build.id} with {created_count} instructions")
                except Exception as finalize_error:
                    logger.warning(f"Failed to finalize demo build: {finalize_error}")
            
            logger.info(f"Created {len(demo.instructions)} instructions for demo data source: {data_source.name}")
        except Exception as e:
            # Log but don't fail - the data source is still created
            logger.warning(f"Failed to create instructions for demo data source {data_source.name}: {e}")

    async def _load_tables(
        self,
        db: AsyncSession,
        data_source: DataSource,
        organization: Organization,
        current_user: User,
    ):
        """
        Load tables from the demo data source and save them.
        
        Uses DataSourceService.save_or_update_tables for consistency.
        """
        from app.services.data_source_service import DataSourceService
        
        try:
            ds_service = DataSourceService()
            await ds_service.save_or_update_tables(
                db=db,
                data_source=data_source,
                organization=organization,
                should_set_active=True,
                current_user=current_user,
            )
            logger.info(f"Loaded tables for demo data source: {data_source.name}")
        except Exception as e:
            # Log but don't fail - the data source is still created
            logger.warning(f"Failed to load tables for demo data source {data_source.name}: {e}")

    async def _get_installed_demos(
        self,
        db: AsyncSession,
        organization_id: str,
    ) -> dict[str, DataSource]:
        """
        Get all installed demo data sources for an organization.
        
        Returns a dict mapping demo_id -> DataSource.
        """
        from sqlalchemy.orm import selectinload
        
        # Query all data sources for this org with their connections
        stmt = select(DataSource).where(
            DataSource.organization_id == organization_id
        ).options(selectinload(DataSource.connections))
        result = await db.execute(stmt)
        data_sources = result.scalars().all()

        # Filter to those that have a demo_id in their config (from connection)
        installed = {}
        for ds in data_sources:
            # Get config from first connection
            conn = ds.connections[0] if ds.connections else None
            if not conn:
                continue
            config = conn.config if isinstance(conn.config, dict) else json.loads(conn.config or "{}")
            demo_id = config.get(DEMO_ID_KEY)
            if demo_id and demo_id in DEMO_DATA_SOURCES:
                installed[demo_id] = ds

        return installed

    async def _get_installed_demo(
        self,
        db: AsyncSession,
        organization_id: str,
        demo_id: str,
    ) -> Optional[DataSource]:
        """
        Check if a specific demo is already installed.
        """
        installed = await self._get_installed_demos(db, organization_id)
        return installed.get(demo_id)
