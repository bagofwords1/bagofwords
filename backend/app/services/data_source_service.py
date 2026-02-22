import importlib
import logging

from app.models.user import User

logger = logging.getLogger(__name__)
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.schemas.data_source_registry import (
    list_available_data_sources,
    config_schema_for,
    default_credentials_schema_for,
    resolve_client_class,
)
from app.models.user_data_source_credentials import UserDataSourceCredentials
from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
from app.models.metadata_resource import MetadataResource
from app.models.metadata_indexing_job import MetadataIndexingJob, IndexingJobStatus
from app.models.git_repository import GitRepository
from app.models.membership import Membership, ROLES_PERMISSIONS

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.schemas.data_source_schema import (
    DataSourceCreate, DataSourceBase, DataSourceSchema, DataSourceUpdate,
    DataSourceMembershipSchema, DataSourceMembershipCreate, DataSourceUserStatus,
    DataSourceListItemSchema, ConnectionEmbedded,
)
from app.schemas.metadata_resource_schema import MetadataResourceSchema

from pydantic import BaseModel
from app.ai.agents.data_source.data_source import DataSourceAgent
from fastapi import HTTPException

import uuid
from uuid import UUID
import json
from datetime import datetime, timezone

from sqlalchemy import insert, delete, or_, and_, func
from sqlalchemy.exc import IntegrityError
from app.schemas.datasource_table_schema import DataSourceTableSchema
from app.models.datasource_table import DataSourceTable  # Add this import at the top of the file
from app.models.user_data_source_overlay import UserDataSourceTable as UserOverlayTable, UserDataSourceColumn as UserOverlayColumn

from typing import List, Dict, Any
from sqlalchemy.orm import selectinload
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import InstructionCreate
from app.core.telemetry import telemetry
from app.ee.audit.service import audit_service

class DataSourceService:

    def __init__(self):
        pass

    async def _build_connections_list(
        self,
        db: AsyncSession,
        data_source: DataSource,
        current_user: User = None,
        live_test: bool = False
    ) -> List[ConnectionEmbedded]:
        """
        Build list of ConnectionEmbedded from all connections of a DataSource.
        Includes user_status if current_user is provided.
        """
        if not data_source.connections:
            return []

        from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
        from app.models.connection_table import ConnectionTable

        connections_list = []

        for conn in data_source.connections:
            # Build user status for the connection
            user_status = None
            if current_user:
                u_svc = UserDataSourceCredentialsService()
                try:
                    user_status = await u_svc.build_user_status_for_connection(
                        db=db,
                        connection=conn,
                        user=current_user,
                        data_source=data_source,
                        live_test=live_test
                    )
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(f"Failed to build user_status for connection {conn.name}: {e}")

            # Get table count for this specific connection
            # Count DataSourceTables that reference ConnectionTables belonging to this connection
            table_count_result = await db.execute(
                select(func.count(DataSourceTable.id))
                .join(ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id)
                .where(
                    DataSourceTable.datasource_id == str(data_source.id),
                    DataSourceTable.is_active == True,
                    ConnectionTable.connection_id == str(conn.id)
                )
            )
            table_count = table_count_result.scalar() or 0

            # Fallback: count legacy tables without connection_table_id
            # This handles data sources created before the ConnectionTable architecture
            if table_count == 0:
                legacy_count_result = await db.execute(
                    select(func.count(DataSourceTable.id))
                    .where(
                        DataSourceTable.datasource_id == str(data_source.id),
                        DataSourceTable.is_active == True,
                        DataSourceTable.connection_table_id == None
                    )
                )
                table_count = legacy_count_result.scalar() or 0

            connections_list.append(ConnectionEmbedded(
                id=str(conn.id),
                name=conn.name,
                type=conn.type,
                auth_policy=conn.auth_policy,
                allowed_user_auth_modes=conn.allowed_user_auth_modes,
                config=conn.config if isinstance(conn.config, dict) else json.loads(conn.config) if conn.config else {},
                is_active=conn.is_active,
                last_synced_at=conn.last_synced_at,
                user_status=user_status,
                table_count=table_count,
            ))

        return connections_list

    async def _create_memberships(self, db: AsyncSession, data_source: DataSource, user_ids: List[str]):
        """
        Create memberships for a list of user IDs.
        """
        if not user_ids:
            return
            
        data_source_memberships = []
        for user_id in user_ids:
            data_source_membership = DataSourceMembership(
                data_source_id=data_source.id,
                principal_type=PRINCIPAL_TYPE_USER,
                principal_id=user_id
            )
            data_source_memberships.append(data_source_membership)
        
        db.add_all(data_source_memberships)
        await db.commit()

    async def create_data_source(self, db: AsyncSession, organization: Organization, current_user: User, data_source: DataSourceCreate):
        # Convert Pydantic model to dict
        data_source_dict = data_source.dict()

        if data_source_dict['name'] == '':
            raise HTTPException(status_code=400, detail="Data source name is required")
        
        # Remove legacy generation flags (generation now deferred to llm_sync after table selection)
        data_source_dict.pop("generate_summary", None)
        data_source_dict.pop("generate_conversation_starters", None)
        data_source_dict.pop("generate_ai_rules", None)
        
        # Extract credentials, config, and membership info
        credentials = data_source_dict.pop("credentials", None)
        config = data_source_dict.pop("config", None)
        is_public = data_source_dict.pop("is_public", False)
        use_llm_sync = data_source_dict.pop("use_llm_sync", False)
        member_user_ids = data_source_dict.pop("member_user_ids", [])
        auth_policy = data_source_dict.get("auth_policy", "system_only")
        
        # Check if linking to existing connection(s)
        connection_id = data_source_dict.pop("connection_id", None)
        connection_ids = data_source_dict.pop("connection_ids", None)
        from app.models.connection import Connection

        # Normalize to list of connection IDs
        existing_connection_ids = []
        if connection_ids and len(connection_ids) > 0:
            existing_connection_ids = connection_ids
        elif connection_id:
            existing_connection_ids = [connection_id]

        # Track connections for linking
        connections_to_link: List[Connection] = []

        if existing_connection_ids:
            # === Mode 2: Link to existing connection(s) ===
            from app.models.connection_table import ConnectionTable
            from app.services.connection_service import ConnectionService

            for conn_id in existing_connection_ids:
                conn_result = await db.execute(
                    select(Connection).filter(
                        Connection.id == conn_id,
                        Connection.organization_id == organization.id
                    )
                )
                conn = conn_result.scalar_one_or_none()
                if not conn:
                    raise HTTPException(status_code=404, detail=f"Connection {conn_id} not found")

                connections_to_link.append(conn)

                # Ensure ConnectionTable is populated (may be empty for legacy connections)
                conn_tables_result = await db.execute(
                    select(func.count(ConnectionTable.id)).filter(ConnectionTable.connection_id == conn_id)
                )
                conn_table_count = conn_tables_result.scalar() or 0

                if conn_table_count == 0 and conn.auth_policy == "system_only":
                    # Refresh schema to populate ConnectionTable from the database
                    connection_service = ConnectionService()
                    await connection_service.refresh_schema(db=db, connection=conn, current_user=current_user)

            # Use first connection's auth_policy for downstream logic
            auth_policy = connections_to_link[0].auth_policy
            ds_type = connections_to_link[0].type

            # Check enterprise license for ALL restricted data sources
            from app.ee.license import is_datasource_allowed
            for conn in connections_to_link:
                if not is_datasource_allowed(conn.type):
                    raise HTTPException(
                        status_code=402,
                        detail=f"The {conn.type} connector requires an enterprise license."
                    )

            # Extract remaining connection fields that won't be used
            data_source_dict.pop("type", None)
            data_source_dict.pop("allowed_user_auth_modes", None)
        else:
            # === Mode 1: Create new connection ===
            # Validate connection and schema access BEFORE saving (for system_only auth)
            if auth_policy == "system_only":
                validation_result = await self.test_new_data_source_connection(
                    db=db, data=data_source, organization=organization, current_user=current_user
                )
                if not validation_result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=validation_result.get("message", "Connection validation failed")
                    )
            
            # Extract connection-related fields
            ds_type = data_source_dict.pop("type", None)
            allowed_user_auth_modes = data_source_dict.pop("allowed_user_auth_modes", None)

            # Check enterprise license for restricted data sources
            from app.ee.license import is_datasource_allowed
            if ds_type and not is_datasource_allowed(ds_type):
                raise HTTPException(
                    status_code=402,
                    detail=f"The {ds_type} connector requires an enterprise license."
                )

            # Auto-generate connection name as type-NUMBER (e.g., postgresql-1)
            from sqlalchemy import func as sql_func
            count_result = await db.execute(
                select(sql_func.count(Connection.id)).filter(
                    Connection.organization_id == organization.id,
                    Connection.type == ds_type
                )
            )
            existing_count = count_result.scalar() or 0
            connection_name = f"{ds_type}-{existing_count + 1}"
            
            # Create the Connection
            new_connection = Connection(
                name=connection_name,
                type=ds_type,
                config=json.dumps(config) if config else "{}",
                organization_id=str(organization.id),
                is_active=True,
                auth_policy=auth_policy,
                allowed_user_auth_modes=allowed_user_auth_modes,
            )
            
            # Encrypt and store credentials on connection
            if credentials:
                new_connection.encrypt_credentials(credentials)
            
            db.add(new_connection)
            await db.flush()  # Get the connection ID
        
        # Create base data source dict (without connection-related fields)
        ds_create_dict = {
            "name": data_source_dict["name"],
            "organization_id": organization.id,
            "is_public": is_public,
            "use_llm_sync": use_llm_sync,
            "owner_user_id": current_user.id
        }
        
        # Create the data source instance
        new_data_source = DataSource(**ds_create_dict)

        # Associate with connection(s)
        if connections_to_link:
            # Mode 2: Link to existing connections
            for conn in connections_to_link:
                new_data_source.connections.append(conn)
        else:
            # Mode 1: New connection created above
            new_data_source.connections.append(new_connection)
        
        db.add(new_data_source)
        try:
            await db.commit()
            await db.refresh(new_data_source)
        except IntegrityError as e:
            # Roll back and surface a friendly conflict error for duplicate names per organization
            await db.rollback()
            name = data_source_dict.get("name") or "data source"
            # SQLite message includes "UNIQUE constraint failed: data_sources.organization_id, data_sources.name"
            # Normalize to a clear API error
            raise HTTPException(
                status_code=409,
                detail=f"A data source named '{name}' already exists in this organization. Please choose a different name."
            )

        # Telemetry: data source created (minimal fields only)
        try:
            await telemetry.capture(
                "data_source_created",
                {
                    "data_source_id": str(new_data_source.id),
                    "type": ds_type,
                    "is_public": bool(is_public),
                    "auth_policy": auth_policy,
                    "use_llm_sync": bool(use_llm_sync),
                    "from_existing_connection": bool(existing_connection_ids),
                    "connection_count": len(connections_to_link) if connections_to_link else 1,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="data_source.created",
                user_id=str(current_user.id),
                resource_type="data_source",
                resource_id=str(new_data_source.id),
                details={"name": new_data_source.name, "type": ds_type, "is_public": bool(is_public), "auth_policy": auth_policy},
            )
        except Exception:
            pass

        # Always add the creator as a member (regardless of public/private status)
        await self._create_memberships(db, new_data_source, [current_user.id])
        
        # Create memberships for additional specified users (only for private data sources)
        if member_user_ids and not is_public:
            # Filter out the creator ID to avoid duplicates
            additional_user_ids = [uid for uid in member_user_ids if uid != current_user.id]
            if additional_user_ids:
                await self._create_memberships(db, new_data_source, additional_user_ids)

        # Save tables (validation already passed above)
        # Note: Description, conversation starters, and instructions are generated
        # later via llm_sync (after user selects tables) to use the correct schema
        if auth_policy == "system_only":
            if connections_to_link:
                # Mode 2: Link to existing connection(s) - use new architecture
                # ConnectionTable was populated above, now sync to DataSourceTable for each connection
                for conn in connections_to_link:
                    await self.sync_domain_tables_from_connection(
                        db, new_data_source, conn,
                        max_auto_select=self.ONBOARDING_MAX_TABLES
                    )
            else:
                # Mode 1: New connection - populate ConnectionTable first, then DataSourceTable
                from app.services.connection_service import ConnectionService
                connection_service = ConnectionService()
                logger.info(f"create_data_source: Mode 1 - populating ConnectionTable for new connection {new_connection.id}")
                await connection_service.refresh_schema(db=db, connection=new_connection, current_user=current_user)
                logger.info(f"create_data_source: Mode 1 - syncing from ConnectionTable to DataSourceTable")
                await self.sync_domain_tables_from_connection(
                    db, new_data_source, new_connection,
                    max_auto_select=self.ONBOARDING_MAX_TABLES
                )
            await db.commit()
            await db.refresh(new_data_source)

        # Reload the data source with relationships to avoid serialization issues
        stmt = (
            select(DataSource)
            .options(
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.connections),
                selectinload(DataSource.tables),
            )
            .where(DataSource.id == new_data_source.id)
        )
        result = await db.execute(stmt)
        final_data_source = result.scalar_one()
        
        # Build connections list
        connections_list = await self._build_connections_list(
            db=db,
            data_source=final_data_source,
            current_user=current_user,
            live_test=False
        )

        # Get first connection for legacy fields
        conn = final_data_source.connections[0] if final_data_source.connections else None
        conn_config = None
        if conn and conn.config:
            conn_config = json.loads(conn.config) if isinstance(conn.config, str) else conn.config

        return DataSourceSchema(
            id=str(final_data_source.id),
            organization_id=str(final_data_source.organization_id),
            name=final_data_source.name,
            created_at=final_data_source.created_at,
            updated_at=final_data_source.updated_at,
            context=final_data_source.context,
            description=final_data_source.description,
            summary=final_data_source.summary,
            conversation_starters=final_data_source.conversation_starters,
            is_active=final_data_source.is_active,
            is_public=final_data_source.is_public,
            use_llm_sync=final_data_source.use_llm_sync,
            owner_user_id=str(final_data_source.owner_user_id) if final_data_source.owner_user_id else None,
            git_repository=final_data_source.git_repository,
            memberships=final_data_source.data_source_memberships,
            connections=connections_list,
            # Legacy fields from first connection for backward compatibility
            type=conn.type if conn else None,
            config=conn_config,
            auth_policy=conn.auth_policy if conn else None,
            allowed_user_auth_modes=conn.allowed_user_auth_modes if conn else None,
            user_status=connections_list[0].user_status if connections_list else None,
        )

    async def generate_data_source_items(self, db: AsyncSession, item: str, data_source_id: str, organization: Organization, current_user: User):
        # get data source by id
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()

        model = await organization.get_default_llm_model(db)
        if not model:
            raise HTTPException(status_code=400, detail="No default LLM model found")

        schema = await self._get_prompt_schema(db=db, data_source=data_source, organization=organization, current_user=current_user)

        data_source_agent = DataSourceAgent(data_source=data_source, schema=schema, model=model)
        response = {}
        if item == "summary":
            response["summary"] = data_source_agent.generate_summary()
        elif item == "conversation_starters":
            response["conversation_starters"] = data_source_agent.generate_conversation_starters()
        elif item == "description":
            response["description"] = data_source_agent.generate_description()

        return response

    async def llm_sync(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User | None = None) -> dict:
        """Run multiple LLM onboarding generators sequentially for a data source.
        Returns a dict of generated fields.
        """
        result: dict = {}

        from app.ai.agents.suggest_instructions.suggest_instructions import SuggestInstructions
        model = await organization.get_default_llm_model(db)
        suggest_instructions = SuggestInstructions(model=model)

        # Load the data source model instance for context and schema sync
        ds_q = await db.execute(
            select(DataSource).filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = ds_q.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")

        # Respect the use_llm_sync flag - if disabled, skip all LLM generation
        if not getattr(data_source, "use_llm_sync", True):
            return {"skipped": True, "reason": "LLM sync disabled for this data source"}


        try:
            summary = await self.generate_data_source_items(db=db, item="summary", data_source_id=data_source_id, organization=organization, current_user=current_user or User())
            result.update(summary)
            # Persist description on the data source if available
            if isinstance(summary, dict) and summary.get("summary"):
                data_source.description = summary.get("summary")
                await db.commit()
                await db.refresh(data_source)
        except Exception:
            pass
        try:
            starters = await self.generate_data_source_items(db=db, item="conversation_starters", data_source_id=data_source_id, organization=organization, current_user=current_user or User())
            result.update(starters)
            # Persist conversation starters on the data source if available
            if isinstance(starters, dict) and starters.get("conversation_starters") is not None:
                data_source.conversation_starters = starters.get("conversation_starters")
                await db.commit()
                await db.refresh(data_source)
        except Exception:
            pass
        # Build a lightweight context view for onboarding suggestions
        try:
            from app.ai.context import ContextHub  # Local import to avoid circular dependency
            context_hub = ContextHub(
                db=db,
                organization=organization,
                report=None,
                data_sources=[data_source],
                user=current_user,
                head_completion=None,
                widget=None,
            )
            await context_hub.prime_static()
            view = context_hub.get_view()
        except Exception:
            view = None

        # Stream onboarding suggestions (non-fatal if fails)
        try:
            created_instruction_payloads: list[dict] = []
            instruction_service = InstructionService()
            
            # === Build System Integration ===
            # Create a single build for all onboarding instructions
            onboarding_build = None
            try:
                from app.services.build_service import BuildService
                build_service = BuildService()
                onboarding_build = await build_service.get_or_create_draft_build(
                    db,
                    organization.id,
                    source='ai',
                    user_id=current_user.id if current_user else None
                )
                logger.info(f"Created onboarding build {onboarding_build.id} for data source {data_source_id}")
            except Exception as build_error:
                logger.warning(f"Failed to create onboarding build: {build_error}")
            
            draft_count = 0
            async for draft in suggest_instructions.onboarding_suggestions(context_view=view):
                draft_count += 1
                text = (draft or {}).get("text")
                category = (draft or {}).get("category")
                if not (text and category):
                    continue
                try:
                    # Create as a draft suggestion (not published)
                    create_payload = InstructionCreate(
                        text=text,
                        category=category,
                        ai_source="onboarding",
                        data_source_ids=[data_source_id],
                        status="draft",
                        global_status="suggested",
                    )
                    created = await instruction_service.create_instruction(
                        db=db,
                        instruction_data=create_payload,
                        current_user=current_user or User(),
                        organization=organization,
                        build=onboarding_build,  # Use shared build
                        auto_finalize=False,  # Don't finalize yet
                    )
                    created_instruction_payloads.append({
                        "id": created.id,
                        "text": created.text,
                        "category": created.category,
                        "status": created.status,
                        "global_status": created.global_status,
                        "data_source_ids": [data_source_id],
                    })
                except Exception as e:
                    # Skip persisting this draft if creation fails
                    logger.debug(f"Failed to create instruction from draft: {e}")
                    continue
            
            logger.debug(f"Onboarding: created {len(created_instruction_payloads)} instructions from {draft_count} drafts")
            
            # === Finalize Build ===
            if onboarding_build and len(created_instruction_payloads) > 0:
                try:
                    await build_service.submit_build(db, onboarding_build.id)
                    await build_service.approve_build(db, onboarding_build.id, approved_by_user_id=current_user.id if current_user else None)
                    await build_service.promote_build(db, onboarding_build.id)
                    logger.info(f"Finalized onboarding build {onboarding_build.id} with {len(created_instruction_payloads)} instructions")
                except Exception as finalize_error:
                    logger.warning(f"Failed to finalize onboarding build: {finalize_error}")
            
            if created_instruction_payloads:
                result["instructions"] = created_instruction_payloads
        except Exception as e:
            logger.debug(f"Onboarding suggestions failed: {e}")

        return result

    # TTL for connection test cache (5 minutes)
    CONNECTION_TEST_TTL_SECONDS = 300

    async def get_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User = None) -> DataSourceSchema:
        from datetime import datetime, timezone
        
        query = (
            select(DataSource)
            .options(
                selectinload(DataSource.git_repository),
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.connections),
                selectinload(DataSource.tables),
            )
            .filter(DataSource.id == data_source_id)
            .filter(DataSource.organization_id == organization.id)
        )
        result = await db.execute(query)
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")

        # Check if we need to retest the connection (cache expired or never tested)
        conn = data_source.connections[0] if data_source.connections else None
        needs_retest = True
        if conn and conn.last_connection_checked_at:
            # Handle timezone-naive datetimes from database
            last_checked = conn.last_connection_checked_at
            if last_checked.tzinfo is None:
                last_checked = last_checked.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last_checked).total_seconds()
            needs_retest = age > self.CONNECTION_TEST_TTL_SECONDS

        # Only test connection if cache is stale
        if needs_retest:
            try:
                await self.test_data_source_connection(
                    db=db,
                    data_source_id=str(data_source.id),
                    organization=organization,
                    current_user=current_user or User(),
                )
                # After commit in test, relationships may be expired; reload with eager options
                try:
                    stmt = (
                        select(DataSource)
                        .options(
                            selectinload(DataSource.git_repository),
                            selectinload(DataSource.data_source_memberships),
                            selectinload(DataSource.connections),
                            selectinload(DataSource.tables),
                        )
                        .where(DataSource.id == data_source.id)
                    )
                    refreshed_res = await db.execute(stmt)
                    data_source = refreshed_res.scalar_one()
                except Exception:
                    pass
            except Exception:
                # Non-fatal: keep serving the resource even if the live check fails
                pass

        # Build connections list - always use cached status (live_test=False)
        # since we already tested if needed above
        connections_list = await self._build_connections_list(
            db=db,
            data_source=data_source,
            current_user=current_user,
            live_test=False
        )

        # Get first connection for legacy fields
        conn = data_source.connections[0] if data_source.connections else None

        # Parse config from connection (may be stored as JSON string)
        conn_config = None
        if conn and conn.config:
            conn_config = json.loads(conn.config) if isinstance(conn.config, str) else conn.config

        schema = DataSourceSchema(
            id=str(data_source.id),
            organization_id=str(data_source.organization_id),
            name=data_source.name,
            created_at=data_source.created_at,
            updated_at=data_source.updated_at,
            context=data_source.context,
            description=data_source.description,
            summary=data_source.summary,
            conversation_starters=data_source.conversation_starters,
            is_active=data_source.is_active,
            is_public=data_source.is_public,
            use_llm_sync=data_source.use_llm_sync,
            owner_user_id=data_source.owner_user_id,
            git_repository=data_source.git_repository,
            memberships=data_source.data_source_memberships,
            connections=connections_list,
            # Legacy fields from first connection for backward compatibility
            type=conn.type if conn else None,
            config=conn_config,
            auth_policy=conn.auth_policy if conn else None,
            allowed_user_auth_modes=conn.allowed_user_auth_modes if conn else None,
            user_status=connections_list[0].user_status if connections_list else None,
        )

        return schema


    async def get_available_data_sources(self, db: AsyncSession, organization: Organization):
        return list_available_data_sources()
    
    async def get_data_sources(self, db: AsyncSession, current_user: User, organization: Organization) -> List[DataSourceListItemSchema]:
        # Query for data sources the user has access to
        # NOTE: Do NOT use selectinload(DataSource.tables) here - it loads ALL tables into memory
        # For data sources with 25K+ tables, this causes severe performance issues
        # Table count is fetched separately via COUNT query in _build_connections_list
        query = (
            select(DataSource)
            .options(
                selectinload(DataSource.git_repository),
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.connections),
            )
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
        result = await db.execute(query)
        data_sources = result.scalars().all()
        # Build list with connection info (no live test for list to keep it fast)
        schemas: list[DataSourceListItemSchema] = []
        for d in data_sources:
            # Build connections list
            connections_list = await self._build_connections_list(
                db=db,
                data_source=d,
                current_user=current_user,
                live_test=False
            )
            conn = d.connections[0] if d.connections else None

            s = DataSourceListItemSchema(
                id=str(d.id),
                name=d.name,
                conversation_starters=getattr(d, "conversation_starters", None),
                description=getattr(d, "description", None),
                created_at=d.created_at,
                status=("active" if bool(d.is_active) else "inactive"),
                connections=connections_list,
                # Legacy fields from first connection for backward compatibility
                type=conn.type if conn else None,
                auth_policy=conn.auth_policy if conn else None,
                user_status=connections_list[0].user_status if connections_list else None,
            )
            schemas.append(s)
        return schemas

    async def get_active_data_sources(self, db: AsyncSession, organization: Organization, current_user: User = None) -> List[DataSourceListItemSchema]:
        """Get all active data sources for an organization that the user has access to, compact list shape"""
        # Build base query for active data sources
        # NOTE: Do NOT use selectinload(DataSource.tables) here - it loads ALL tables into memory
        # For data sources with 25K+ tables, this causes severe performance issues
        # Table count is fetched separately via COUNT query in _build_connections_list
        stmt = (
            select(DataSource)
            .options(
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.connections),
            )
            .where(
                DataSource.organization_id == organization.id,
                DataSource.is_active == True
            )
        )
        
        # Apply access control if user is provided (same logic as get_data_sources)
        if current_user:
            stmt = stmt.filter(
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
            
        result = await db.execute(stmt)
        data_sources = result.scalars().all()
        
        # Compute once whether the current user has org-level permission to update data sources
        has_update_perm = False
        if current_user:
            try:
                mem_res = await db.execute(
                    select(Membership).where(
                        Membership.user_id == current_user.id,
                        Membership.organization_id == organization.id,
                    )
                )
                membership = mem_res.scalar_one_or_none()
                has_update_perm = bool(membership and "update_data_source" in ROLES_PERMISSIONS.get(membership.role, set()))
            except Exception:
                has_update_perm = False
        
        items: list[DataSourceListItemSchema] = []
        for d in data_sources:
            # Build connections list
            connections_list = await self._build_connections_list(
                db=db,
                data_source=d,
                current_user=current_user,
                live_test=False
            )
            conn = d.connections[0] if d.connections else None

            s = DataSourceListItemSchema(
                id=str(d.id),
                name=d.name,
                conversation_starters=getattr(d, "conversation_starters", None),
                description=getattr(d, "description", None),
                created_at=d.created_at,
                status=("active" if bool(d.is_active) else "inactive"),
                connections=connections_list,
                # Legacy fields from first connection for backward compatibility
                type=conn.type if conn else None,
                auth_policy=conn.auth_policy if conn else None,
                user_status=connections_list[0].user_status if connections_list else None,
            )

            # Exclude user_required data sources lacking user credentials,
            # unless the user has permission to update data sources (admin/editor)
            auth_policy = conn.auth_policy if conn else "system_only"
            if auth_policy == "user_required" and current_user:
                try:
                    has_user_creds = getattr(s.user_status, "has_user_credentials", False)
                except Exception:
                    has_user_creds = False
                if not has_user_creds and not has_update_perm:
                    continue
            items.append(s)
        return items

    async def get_public_data_sources(self, db: AsyncSession, organization: Organization) -> List[DataSourceListItemSchema]:
        """
        Get only public active data sources with system_only auth for an organization.
        Used for Slack channel mentions where we can't rely on individual user credentials.
        Only includes data sources that use system-level credentials (auth_policy="system_only").
        """
        stmt = (
            select(DataSource)
            .options(
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.connections),
            )
            .where(
                DataSource.organization_id == organization.id,
                DataSource.is_active == True,
                DataSource.is_public == True  # Only public data sources
            )
        )

        result = await db.execute(stmt)
        data_sources = result.scalars().all()

        items: list[DataSourceListItemSchema] = []
        for d in data_sources:
            conn = d.connections[0] if d.connections else None
            # Only include data sources with system_only auth policy
            # Skip user_required data sources since channel mentions can't use individual user credentials
            auth_policy = conn.auth_policy if conn else "system_only"
            if auth_policy == "user_required":
                continue

            connections_list = await self._build_connections_list(
                db=db,
                data_source=d,
                current_user=None,
                live_test=False
            )

            s = DataSourceListItemSchema(
                id=str(d.id),
                name=d.name,
                conversation_starters=getattr(d, "conversation_starters", None),
                description=getattr(d, "description", None),
                created_at=d.created_at,
                status=("active" if bool(d.is_active) else "inactive"),
                connections=connections_list,
                type=conn.type if conn else None,
                auth_policy=auth_policy,
                user_status=connections_list[0].user_status if connections_list else None,
            )
            items.append(s)
        return items

    async def get_data_source_fields(self, db: AsyncSession, data_source_type: str, organization: Organization, current_user: User, auth_type: str | None = None, auth_policy: str | None = None):
        try:
            # Resolve schemas via registry
            config_schema = config_schema_for(data_source_type)
            from app.schemas.data_source_registry import credentials_schema_for, get_entry
            entry = get_entry(data_source_type)
            # Filter auth variants by policy if provided (system_only vs user_required)
            def allowed(mode: str) -> bool:
                try:
                    scopes = (entry.credentials_auth.by_auth.get(mode) or {}).scopes or []
                except Exception:
                    scopes = []
                if not auth_policy or auth_policy == "system_only":
                    return "system" in scopes
                if auth_policy == "user_required":
                    return "user" in scopes
                return True
            # Build config fields
            config_fields = self._extract_fields_from_schema(schema=config_schema)
            # Build credentials fields for default and for all auth modes
            # If a policy is specified and the chosen auth_type is not allowed, drop it so default applies
            if auth_type and not allowed(auth_type):
                auth_type = None
            default_credentials_schema = credentials_schema_for(data_source_type, auth_type)
            credentials_fields = self._extract_fields_from_schema(schema=default_credentials_schema)
            credentials_by_auth: dict[str, dict] = {}
            for mode, variant in (entry.credentials_auth.by_auth or {}).items():
                if not allowed(mode):
                    continue
                try:
                    credentials_by_auth[mode] = self._extract_fields_from_schema(schema=variant.schema)
                except Exception:
                    continue
            # Get titles/descriptions and auth metadata
            catalog = {d.get("type"): d for d in list_available_data_sources()}
            meta = catalog.get(data_source_type) or {}
            return {
                "config": config_fields,
                "credentials": credentials_fields,
                "credentials_by_auth": credentials_by_auth,
                "type": data_source_type,
                "title": meta.get("title"),
                "description": meta.get("description"),
                "auth": {
                    "default": entry.credentials_auth.default,
                    "by_auth": {k: {"title": v.title} for k, v in (entry.credentials_auth.by_auth or {}).items() if allowed(k)},
                    "policy": auth_policy or "system_only",
                },
            }
        except Exception as e:
            raise ValueError(f"Schema not found for {data_source_type}: {str(e)}")
    
    async def delete_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")

        # Capture details before deletion for audit
        data_source_name = data_source.name

        # 1) Delete per-user overlay columns and tables (they hard-FK the data source)
        #    Delete columns via subquery of overlay table ids, then overlay tables.
        overlay_ids_subq = select(UserOverlayTable.id).where(UserOverlayTable.data_source_id == data_source_id)
        await db.execute(
            delete(UserOverlayColumn).where(
                UserOverlayColumn.user_data_source_table_id.in_(overlay_ids_subq)
            )
        )
        await db.execute(
            delete(UserOverlayTable).where(UserOverlayTable.data_source_id == data_source_id)
        )

        # 2) Remove direct child rows managed by ORM on update but not guaranteed by DB cascades
        await db.execute(
            delete(DataSourceMembership).where(DataSourceMembership.data_source_id == data_source_id)
        )
        await db.execute(
            delete(UserDataSourceCredentials).where(UserDataSourceCredentials.data_source_id == data_source_id)
        )

        # 3) Delete dependent metadata resources first (they FK both data source and jobs)
        resources_q = await db.execute(
            select(MetadataResource).where(MetadataResource.data_source_id == data_source_id)
        )
        for resource in resources_q.scalars().all():
            await db.delete(resource)

        # 4) Delete metadata indexing jobs for this data source
        jobs_q = await db.execute(
            select(MetadataIndexingJob).where(MetadataIndexingJob.data_source_id == data_source_id)
        )
        for job in jobs_q.scalars().all():
            await db.delete(job)

        # 5) Delete any linked git repository for this data source
        repo_q = await db.execute(
            select(GitRepository).where(
                GitRepository.data_source_id == data_source_id,
                GitRepository.organization_id == organization.id,
            )
        )
        repo = repo_q.scalar_one_or_none()
        if repo:
            await db.delete(repo)

        # Apply deletions before removing the data source to avoid NULLing non-nullable FKs
        await db.commit()

        # 6) Delete schema tables associated with this data source (commits internally)
        await self.delete_data_source_tables(db=db, data_source_id=data_source_id, organization=organization, current_user=current_user)

        # 7) Finally delete the data source
        await db.delete(data_source)
        await db.commit()

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="data_source.deleted",
                user_id=str(current_user.id),
                resource_type="data_source",
                resource_id=str(data_source_id),
                details={"name": data_source_name},
            )
        except Exception:
            pass

        return {"message": "Data source deleted successfully"}

    async def delete_data_source_tables(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        result = await db.execute(select(DataSourceTable).filter(DataSourceTable.datasource_id == data_source_id))
        tables = result.scalars().all()
        for table in tables:
            await db.delete(table)
        await db.commit()
        return {"message": "Data source tables deleted successfully"}
    
    async def test_data_source_connection(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        from datetime import datetime, timezone
        from sqlalchemy.orm import selectinload
        
        try:
            # Find the data source with connections eager-loaded
            result = await db.execute(
                select(DataSource)
                .options(selectinload(DataSource.connections))
                .filter(
                    DataSource.id == data_source_id, 
                    DataSource.organization_id == organization.id
                )
            )
            data_source = result.scalar_one_or_none()
            if not data_source:
                raise ValueError(f"Data source not found: {data_source_id}")

            # Get the matching client from DATA_SOURCE_DETAILS
            # Import and instantiate the client class
            # Resolve client with policy-aware credentials
            client = await self.construct_client(db=db, data_source=data_source, current_user=current_user)
            # Test the connection
            connection_status = client.test_connection()

            # Normalize success value for robust handling
            try:
                success = bool(connection_status.get("success")) if isinstance(connection_status, dict) else bool(connection_status)
            except Exception:
                success = False

            # Cache the connection test result on the Connection model
            conn = data_source.connections[0] if data_source.connections else None
            if conn:
                conn.last_connection_status = "success" if success else "not_connected"
                conn.last_connection_checked_at = datetime.utcnow()

            # Reflect connectivity on org-wide flag only for system creds
            if getattr(data_source, "auth_policy", "system_only") == "system_only":
                if not success:
                    data_source.is_active = False
                else:
                    if data_source.is_active == False:
                        data_source.is_active = True
            
            await db.commit()
            if conn:
                await db.refresh(conn)
            await db.refresh(data_source)

        except Exception as e:
            # For system creds, mark DS inactive; for user creds, don't flip org state
            try:
                if 'data_source' in locals():
                    # Cache the failure
                    conn = data_source.connections[0] if data_source.connections else None
                    if conn:
                        conn.last_connection_status = "not_connected"
                        conn.last_connection_checked_at = datetime.utcnow()
                    
                    if getattr(data_source, "auth_policy", "system_only") == "system_only":
                        data_source.is_active = False
                    await db.commit()
            except Exception:
                pass

            # Return the error message instead of True
            connection_status = {
                "success": False,
                "message": str(e)
            }
        
        return connection_status
    
    async def test_new_data_source_connection(self, db: AsyncSession, data: DataSourceCreate, organization: Organization, current_user: User):
        """Test connection for a new (unsaved) data source using DataSourceCreate payload.
        Validates both basic connectivity AND schema access (get_tables).
        Does not persist anything to the database.
        """
        try:
            payload = data.dict()
            data_source_type = payload.get("type")
            config = payload.get("config") or {}
            credentials = payload.get("credentials") or {}

            # Instantiate client by type using same naming convention as DataSource.get_client
            client = self._resolve_client_by_type(
                data_source_type=data_source_type,
                config=config,
                credentials=credentials,
            )

            # Step 1: Test basic connectivity
            connection_status = client.test_connection()
            if not connection_status.get("success"):
                return connection_status

            # Step 2: Validate schema access by attempting to get tables
            schema_status = self._validate_schema_access(client)
            
            # Combine results
            if not schema_status.get("success"):
                return {
                    "success": False,
                    "message": schema_status.get("message", "Schema validation failed"),
                    "connectivity": True,
                    "schema_access": False,
                    "table_count": 0,
                }
            
            return {
                "success": True,
                "message": f"Connected successfully. Found {schema_status.get('table_count', 0)} tables.",
                "connectivity": True,
                "schema_access": True,
                "table_count": schema_status.get("table_count", 0),
            }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
                "connectivity": False,
                "schema_access": False,
            }

    def _validate_schema_access(self, client) -> dict:
        """Validate that we can read schema metadata and find tables.
        Returns a dict with success status, table count, and optional error message.
        """
        try:
            # Try get_schemas first (most clients), fall back to get_tables
            tables = None
            if hasattr(client, "get_schemas"):
                tables = client.get_schemas()
            elif hasattr(client, "get_tables"):
                tables = client.get_tables()
            
            if tables is None:
                return {
                    "success": False,
                    "message": "Client does not support schema introspection",
                    "table_count": 0,
                }
            
            table_count = len(tables) if tables else 0
            
            # Note: Empty databases are allowed - schema can be refreshed later when tables are added
            return {
                "success": True,
                "table_count": table_count,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Connected but cannot read schema: {str(e)}",
                "table_count": 0,
            }

    async def resolve_credentials(self, db: AsyncSession, data_source: DataSource, current_user: User | None) -> dict:
        # Get connection from data source
        conn = data_source.connections[0] if data_source.connections else None
        if not conn:
            return {}
        
        # system_only → use stored system credentials
        if conn.auth_policy == "system_only":
            try:
                return conn.decrypt_credentials() or {}
            except Exception:
                return {}
        
        # user_required → require per-user credentials
        if not current_user:
            raise HTTPException(status_code=403, detail="User credentials required")
        row = await db.execute(
            select(UserDataSourceCredentials)
            .where(
                UserDataSourceCredentials.data_source_id == data_source.id,
                UserDataSourceCredentials.user_id == current_user.id,
                UserDataSourceCredentials.is_active == True,
            )
            .order_by(UserDataSourceCredentials.is_primary.desc(), UserDataSourceCredentials.updated_at.desc())
        )
        row = row.scalars().first()
        if not row:
            # Owner/admin fallback: allow creator or admin to use system creds if present
            try:
                is_owner = str(getattr(data_source, "owner_user_id", "")) == str(getattr(current_user, "id", ""))
            except Exception:
                is_owner = False
            # Check org role permission for update_data_source
            has_update_perm = False
            try:
                mem_res = await db.execute(
                    select(Membership).where(
                        Membership.user_id == current_user.id,
                        Membership.organization_id == getattr(data_source, "organization_id", None),
                    )
                )
                membership = mem_res.scalar_one_or_none()
                has_update_perm = bool(membership and "update_data_source" in ROLES_PERMISSIONS.get(membership.role, set()))
            except Exception:
                has_update_perm = False
            if is_owner or has_update_perm:
                # Owner/admin can use system credentials (or empty creds for data sources like SQLite)
                if conn.credentials:
                    try:
                        return conn.decrypt_credentials() or {}
                    except Exception:
                        pass
                # Return empty dict for data sources that don't require credentials (e.g., SQLite)
                return {}
            raise HTTPException(status_code=403, detail="User credentials required for this data source")
        return row.decrypt_credentials() or {}

    async def construct_client(self, db: AsyncSession, data_source: DataSource, current_user: User | None):
        """
        Construct a single client for the first connection.
        DEPRECATED: Use construct_clients() for multi-connection support.
        """
        # Get connection from data source
        if not data_source.connections:
            raise HTTPException(status_code=400, detail="Data source has no associated connection")

        conn = data_source.connections[0]

        # Resolve client class from registry (no model dependency)
        ClientClass = resolve_client_class(conn.type)
        # Merge config and creds
        config = json.loads(conn.config) if isinstance(conn.config, str) else (conn.config or {})
        creds = await self.resolve_credentials(db=db, data_source=data_source, current_user=current_user)
        params = {**(config or {}), **(creds or {})}
        # Strip meta keys
        meta_keys = {"auth_type", "auth_policy", "allowed_user_auth_modes"}
        params = {k: v for k, v in (params or {}).items() if v is not None and k not in meta_keys}
        # Narrow to constructor signature
        try:
            import inspect
            sig = inspect.signature(ClientClass.__init__)
            allowed = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}
        except Exception:
            allowed = params
        return ClientClass(**allowed)

    async def construct_clients(self, db: AsyncSession, data_source: DataSource, current_user: User | None) -> Dict[str, Any]:
        """
        Construct clients for ALL connections in the domain.

        Returns:
            Dict keyed by "{domain_name}:{connection_name}" -> client

        For backward compatibility with legacy code, also adds aliases:
        - "{domain_name}" (only if single connection, for legacy ds_clients.get("name") pattern)
        """
        import inspect
        from typing import Dict, Any

        if not data_source.connections:
            raise HTTPException(status_code=400, detail="Data source has no associated connections")

        clients: Dict[str, Any] = {}

        for conn in data_source.connections:
            key = f"{data_source.name}:{conn.name}"

            # Resolve client class from registry
            ClientClass = resolve_client_class(conn.type)

            # Merge config and creds
            config = json.loads(conn.config) if isinstance(conn.config, str) else (conn.config or {})

            # Resolve credentials for this specific connection
            creds = await self.resolve_credentials_for_connection(
                db=db,
                connection=conn,
                data_source=data_source,
                current_user=current_user
            )

            params = {**(config or {}), **(creds or {})}

            # Strip meta keys
            meta_keys = {"auth_type", "auth_policy", "allowed_user_auth_modes"}
            params = {k: v for k, v in (params or {}).items() if v is not None and k not in meta_keys}

            # Narrow to constructor signature
            try:
                sig = inspect.signature(ClientClass.__init__)
                allowed = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}
            except Exception:
                allowed = params

            clients[key] = ClientClass(**allowed)

        # Backward compatibility: add legacy key aliases for single-connection domains
        # This supports old code patterns like ds_clients.get("domain_name") or ds_clients["domain_name"]
        if len(data_source.connections) == 1:
            first_key = next(iter(clients.keys()))
            first_client = clients[first_key]
            # Add domain name as alias
            clients[data_source.name] = first_client

        return clients

    async def resolve_credentials_for_connection(
        self,
        db: AsyncSession,
        connection,  # Connection model
        data_source: DataSource,
        current_user: User | None
    ) -> dict:
        """
        Resolve credentials for a specific connection.
        Falls back to system credentials stored on the connection.
        """
        auth_policy = connection.auth_policy or "system_only"

        # For user_required, try to get user credentials first
        if auth_policy == "user_required" and current_user:
            from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
            u_svc = UserDataSourceCredentialsService()
            try:
                row = await u_svc.get_primary_active_row(db, data_source, current_user)
                if row:
                    return row.decrypt_credentials() or {}
            except Exception:
                pass

            # Check if owner or admin can use system credentials
            is_owner = str(getattr(data_source, "owner_user_id", "")) == str(getattr(current_user, "id", ""))
            has_update_perm = False
            try:
                from app.models.membership import Membership, ROLES_PERMISSIONS
                mem_res = await db.execute(
                    select(Membership).where(
                        Membership.user_id == current_user.id,
                        Membership.organization_id == getattr(data_source, "organization_id", None),
                    )
                )
                membership = mem_res.scalar_one_or_none()
                has_update_perm = bool(membership and "update_data_source" in ROLES_PERMISSIONS.get(membership.role, set()))
            except Exception:
                pass

            if is_owner or has_update_perm:
                # Owner/admin can use system credentials
                return connection.get_credentials() if hasattr(connection, 'get_credentials') else {}

            raise HTTPException(status_code=403, detail="User credentials required for this connection")

        # For system_only or if no user, use system credentials
        return connection.get_credentials() if hasattr(connection, 'get_credentials') else {}

    def _resolve_client_by_type(self, data_source_type: str, config: dict, credentials: dict):
        """Dynamically import and construct the client for a given data source type.
        Mirrors the naming convention used in DataSource.get_client().
        """
        if not data_source_type:
            raise ValueError("Data source type is required")
        try:
            module_name = f"app.data_sources.clients.{data_source_type.lower()}_client"
            title = "".join(word[:1].upper() + word[1:] for word in data_source_type.split("_"))
            class_name = f"{title}Client"

            module = importlib.import_module(module_name)
            ClientClass = getattr(module, class_name)

            client_params = (config or {}).copy()
            if credentials:
                client_params.update(credentials)

            # Strip meta keys (e.g., auth_type) that are not part of client signatures
            meta_keys = {"auth_type", "auth_policy", "allowed_user_auth_modes"}
            client_params = {k: v for k, v in (client_params or {}).items() if k not in meta_keys}

            return ClientClass(**client_params)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Unable to load data source client for {data_source_type}: {str(e)}")
    
    async def update_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization, data_source: DataSourceUpdate, current_user: User):
        result = await db.execute(
            select(DataSource)
            .options(
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.connections)
            )
            .filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id)
        )
        data_source_db = result.scalar_one_or_none()
        
        if not data_source_db:
            raise HTTPException(status_code=404, detail="Data source not found")

        # Extract the update data
        update_data = data_source.dict(exclude_unset=True)
        
        # Detect if connection-relevant fields are being changed
        connection_fields = {'config', 'credentials', 'auth_policy'}
        connection_updates = {k: update_data.pop(k) for k in list(update_data.keys()) if k in connection_fields}
        connection_changed = bool(connection_updates)
        
        # Handle membership updates
        if 'member_user_ids' in update_data:
            member_user_ids = update_data.pop('member_user_ids')
            if member_user_ids is not None:
                # Delete existing data_source_memberships
                await db.execute(
                    delete(DataSourceMembership).where(
                        DataSourceMembership.data_source_id == data_source_id
                    )
                )
                # Create new data_source_memberships
                if member_user_ids:
                    await self._create_memberships(db, data_source_db, member_user_ids)
        
        # Update remaining domain-specific fields on DataSource
        for field, value in update_data.items():
            if value is not None:
                setattr(data_source_db, field, value)
        
        # Delegate connection-relevant field updates to Connection
        if connection_changed and data_source_db.connections:
            from app.services.connection_service import ConnectionService
            conn_svc = ConnectionService()
            conn = data_source_db.connections[0]
            
            await conn_svc.update_connection(
                db=db,
                connection_id=str(conn.id),
                organization=organization,
                current_user=current_user,
                **connection_updates
            )
        
        try:
            await db.commit()
            
            # Refresh tables if connection fields changed
            if connection_changed and data_source_db.connections:
                conn = data_source_db.connections[0]
                if conn.auth_policy == "system_only":
                    try:
                        from app.services.connection_service import ConnectionService
                        conn_svc = ConnectionService()
                        await conn_svc.refresh_schema(db, conn, current_user)
                    except Exception:
                        # Non-fatal: tables refresh can fail without blocking update
                        pass
            
            # Reload the data source with relationships to avoid serialization issues
            stmt = (
                select(DataSource)
                .options(
                    selectinload(DataSource.data_source_memberships),
                    selectinload(DataSource.connections),
                    selectinload(DataSource.git_repository)
                )
                .where(DataSource.id == data_source_db.id)
            )
            result = await db.execute(stmt)
            final_data_source = result.scalar_one()

            # Audit log
            try:
                await audit_service.log(
                    db=db,
                    organization_id=str(organization.id),
                    action="data_source.updated",
                    user_id=str(current_user.id),
                    resource_type="data_source",
                    resource_id=str(final_data_source.id),
                    details={"name": final_data_source.name},
                )
            except Exception:
                pass

            # Return schema with connection info
            return await self.get_data_source(db, str(final_data_source.id), organization, current_user)
        except IntegrityError as e:
            await db.rollback()
            # Conflict on unique constraint (likely name within organization)
            raise HTTPException(
                status_code=409,
                detail="Another data source with this name already exists in this organization."
            )
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update data source: {str(e)}")

    def _extract_fields_from_schema(self, schema: BaseModel):
        main_model_schema = schema.model_json_schema()  # (1)!
        
        # Debug logging
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f"Extracted schema: {main_model_schema}")

        return main_model_schema

    async def get_data_source_fresh_schema(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User = None):
        result = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id)
        )
        data_source = result.scalar_one_or_none()

        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")


        client = await self.construct_client(db=db, data_source=data_source, current_user=current_user)
        try:
            schema = client.get_schemas()
            # Empty list is valid (e.g., empty database) - only None indicates an error
            if schema is None:
                raise HTTPException(status_code=500, detail="No schema returned from data source")
            return schema
        except HTTPException:
            raise
        except Exception as e:
            print(f"Error getting data source schema: {e}")
            raise HTTPException(status_code=500, detail=f"Error getting data source schema: {e}")
    
    async def get_data_source_schema(self, db: AsyncSession, data_source_id: str, include_inactive: bool = False, organization: Organization = None, current_user: User = None, with_stats: bool = False):
        result = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id)
        )
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        # Get auth_policy from the first connection (auth_policy is now on Connection, not DataSource)
        auth_policy = "system_only"
        if data_source.connections:
            auth_policy = data_source.connections[0].auth_policy or "system_only"
            
        # For user_required policy, prefer the user's live schema view and upsert overlay
        if auth_policy == "user_required" and current_user is not None:
            try:
                return await self.get_user_data_source_schema(db=db, data_source=data_source, user=current_user)
            except Exception:
                # Fallback to canonical schema if user overlay fetch fails
                pass

        schemas = await data_source.get_schemas(db=db, include_inactive=include_inactive, with_stats=with_stats)

        return schemas

    async def get_data_source_schema_paginated(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        page: int = 1,
        page_size: int = 100,
        schema_filter: List[str] = None,
        connection_filter: List[str] = None,
        search: str = None,
        sort_by: str = "is_active",
        sort_dir: str = "desc",
        include_inactive: bool = True,
        selected_state: str = None,  # 'selected', 'unselected', or None for all
        with_stats: bool = False,
        current_user: User = None,
    ):
        """
        Get paginated tables for a data source with filtering and sorting.
        Returns PaginatedTablesResponse with tables, counts, and metadata.
        """
        from app.schemas.datasource_table_schema import PaginatedTablesResponse, DataSourceTableSchema, ConnectionInfo
        from app.models.connection_table import ConnectionTable
        from app.models.connection import Connection
        from sqlalchemy import func, case
        import math
        
        # Verify data source exists
        result = await db.execute(
            select(DataSource).filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = result.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        # Get total_tables count first (no filters - for display purposes)
        total_tables_result = await db.execute(
            select(func.count(DataSourceTable.id)).where(DataSourceTable.datasource_id == data_source_id)
        )
        total_tables = total_tables_result.scalar() or 0
        
        # Build base query
        base_query = select(DataSourceTable).where(DataSourceTable.datasource_id == data_source_id)
        count_query = select(func.count(DataSourceTable.id)).where(DataSourceTable.datasource_id == data_source_id)
        
        # Apply selected_state filter (takes precedence over include_inactive)
        if selected_state == 'selected':
            base_query = base_query.where(DataSourceTable.is_active == True)
            count_query = count_query.where(DataSourceTable.is_active == True)
        elif selected_state == 'unselected':
            base_query = base_query.where(DataSourceTable.is_active == False)
            count_query = count_query.where(DataSourceTable.is_active == False)
        elif not include_inactive:
            # Only apply include_inactive if selected_state is not set
            base_query = base_query.where(DataSourceTable.is_active == True)
            count_query = count_query.where(DataSourceTable.is_active == True)
        
        # Helper for cross-database JSON schema extraction
        # SQLite uses json_extract, PostgreSQL uses ->> operator
        def get_schema_expr():
            bind = db.get_bind()
            dialect_name = bind.dialect.name if bind else "sqlite"
            if dialect_name == "postgresql":
                # PostgreSQL: use ->> operator for JSON text extraction
                return DataSourceTable.metadata_json.op('->>')('schema')
            else:
                # SQLite: use json_extract
                return func.json_extract(DataSourceTable.metadata_json, '$.schema')
        
        # Apply schema filter (from metadata_json->>'schema')
        if schema_filter and len(schema_filter) > 0:
            # Filter by schema names in metadata_json
            schema_expr = get_schema_expr()
            schema_conditions = [schema_expr == schema_name for schema_name in schema_filter]
            if schema_conditions:
                from sqlalchemy import or_
                base_query = base_query.where(or_(*schema_conditions))
                count_query = count_query.where(or_(*schema_conditions))

        # Apply connection filter (via connection_table -> connection relationship)
        if connection_filter and len(connection_filter) > 0:
            # Join with ConnectionTable to filter by connection_id
            base_query = base_query.join(
                ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id
            ).where(ConnectionTable.connection_id.in_(connection_filter))
            count_query = count_query.join(
                ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id
            ).where(ConnectionTable.connection_id.in_(connection_filter))

        # Apply search filter
        if search and search.strip():
            search_pattern = f"%{search.strip().lower()}%"
            base_query = base_query.where(func.lower(DataSourceTable.name).like(search_pattern))
            count_query = count_query.where(func.lower(DataSourceTable.name).like(search_pattern))
        
        # Get total count matching filter
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0
        
        # Get total selected count (across ALL tables, not just filtered)
        selected_count_result = await db.execute(
            select(func.count(DataSourceTable.id)).where(
                DataSourceTable.datasource_id == data_source_id,
                DataSourceTable.is_active == True
            )
        )
        selected_count = selected_count_result.scalar() or 0
        
        # Get distinct schemas for filter dropdown (database-agnostic)
        schema_expr = get_schema_expr()
        schemas_result = await db.execute(
            select(func.distinct(schema_expr))
            .where(DataSourceTable.datasource_id == data_source_id)
            .where(schema_expr.isnot(None))
        )
        distinct_schemas = [row[0] for row in schemas_result.fetchall() if row[0]]

        # Get distinct connections for filter dropdown
        connections_result = await db.execute(
            select(Connection.id, Connection.name, Connection.type)
            .select_from(DataSourceTable)
            .join(ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id)
            .join(Connection, ConnectionTable.connection_id == Connection.id)
            .where(DataSourceTable.datasource_id == data_source_id)
            .distinct()
        )
        distinct_connections = [
            ConnectionInfo(id=str(row[0]), name=row[1], type=row[2])
            for row in connections_result.fetchall()
        ]

        # Apply sorting
        sort_column = DataSourceTable.name  # default
        if sort_by == "centrality_score":
            sort_column = DataSourceTable.centrality_score
        elif sort_by == "is_active":
            sort_column = DataSourceTable.is_active
        elif sort_by == "richness":
            sort_column = DataSourceTable.richness
        
        if sort_dir.lower() == "desc":
            base_query = base_query.order_by(sort_column.desc().nullslast())
        else:
            base_query = base_query.order_by(sort_column.asc().nullsfirst())
        
        # Apply pagination
        offset = (page - 1) * page_size
        base_query = base_query.offset(offset).limit(page_size)

        # Add selectinload for connection info
        base_query = base_query.options(
            selectinload(DataSourceTable.connection_table).selectinload(ConnectionTable.connection)
        )

        # Execute query
        tables_result = await db.execute(base_query)
        table_rows = tables_result.scalars().all()
        
        # Fetch stats if requested
        stats_map = {}
        if with_stats:
            from app.models.table_stats import TableStats
            stats_result = await db.execute(
                select(TableStats).where(
                    TableStats.report_id == None,
                    TableStats.data_source_id == data_source_id,
                )
            )
            for s in stats_result.scalars().all():
                stats_map[(s.table_fqn or '').lower()] = s
        
        # Convert to schema objects
        tables = []
        for table in table_rows:
            # Get stats for this table
            stats = stats_map.get((table.name or '').lower()) if with_stats else None

            # Extract connection info from relationship
            conn_id = None
            conn_name = None
            conn_type = None
            if table.connection_table and table.connection_table.connection:
                conn = table.connection_table.connection
                conn_id = str(conn.id)
                conn_name = conn.name
                conn_type = conn.type

            table_schema = DataSourceTableSchema(
                id=str(table.id),
                name=table.name,
                columns=table.columns or [],
                no_rows=table.no_rows or 0,
                datasource_id=str(table.datasource_id),
                pks=table.pks or [],
                fks=table.fks or [],
                is_active=table.is_active,
                metadata_json=table.metadata_json,
                # Connection info
                connection_id=conn_id,
                connection_name=conn_name,
                connection_type=conn_type,
                # Metrics
                centrality_score=table.centrality_score,
                richness=table.richness,
                degree_in=table.degree_in,
                degree_out=table.degree_out,
                entity_like=table.entity_like,
                metrics_computed_at=table.metrics_computed_at.isoformat() if table.metrics_computed_at else None,
                # Stats fields
                usage_count=int(stats.usage_count or 0) if stats else None,
                success_count=int(stats.success_count or 0) if stats else None,
                failure_count=int(stats.failure_count or 0) if stats else None,
                pos_feedback_count=int(stats.pos_feedback_count or 0) if stats else None,
                neg_feedback_count=int(stats.neg_feedback_count or 0) if stats else None,
            )
            tables.append(table_schema)
        
        total_pages = math.ceil(total / page_size) if page_size > 0 else 0
        
        return PaginatedTablesResponse(
            tables=tables,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            schemas=sorted(distinct_schemas),
            connections=distinct_connections,
            selected_count=selected_count,
            total_tables=total_tables,
            has_more=page < total_pages,
        )

    async def bulk_update_tables_status(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        action: str,
        filter_params: dict = None,
        current_user: User = None,
    ):
        """
        Bulk update is_active status for tables matching filter.
        action: "activate" or "deactivate"
        filter_params: {"schema": ["schema1", "schema2"], "search": "..."}
        """
        from sqlalchemy import update, func
        from app.schemas.datasource_table_schema import DeltaUpdateTablesResponse
        
        # Verify data source exists
        result = await db.execute(
            select(DataSource).filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = result.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        if action not in ("activate", "deactivate"):
            raise HTTPException(status_code=400, detail="Action must be 'activate' or 'deactivate'")
        
        new_status = action == "activate"
        
        # Build update query with filters
        update_query = (
            update(DataSourceTable)
            .where(DataSourceTable.datasource_id == data_source_id)
        )
        
        filter_params = filter_params or {}
        
        # Apply schema filter (database-agnostic JSON extraction)
        schema_filter = filter_params.get("schema") or filter_params.get("schemas")
        if schema_filter:
            if isinstance(schema_filter, str):
                schema_filter = [schema_filter]
            if len(schema_filter) > 0:
                from sqlalchemy import or_
                # Detect dialect for cross-database JSON extraction
                bind = db.get_bind()
                dialect_name = bind.dialect.name if bind else "sqlite"
                if dialect_name == "postgresql":
                    schema_expr = DataSourceTable.metadata_json.op('->>')('schema')
                else:
                    schema_expr = func.json_extract(DataSourceTable.metadata_json, '$.schema')
                schema_conditions = [schema_expr == s for s in schema_filter]
                update_query = update_query.where(or_(*schema_conditions))
        
        # Apply search filter
        search = filter_params.get("search")
        if search and search.strip():
            search_pattern = f"%{search.strip().lower()}%"
            update_query = update_query.where(func.lower(DataSourceTable.name).like(search_pattern))
        
        # Execute update
        update_query = update_query.values(is_active=new_status)
        result = await db.execute(update_query)
        await db.commit()
        
        affected_count = result.rowcount
        
        # Get new total selected count
        selected_count_result = await db.execute(
            select(func.count(DataSourceTable.id)).where(
                DataSourceTable.datasource_id == data_source_id,
                DataSourceTable.is_active == True
            )
        )
        total_selected = selected_count_result.scalar() or 0
        
        return DeltaUpdateTablesResponse(
            activated_count=affected_count if new_status else 0,
            deactivated_count=affected_count if not new_status else 0,
            total_selected=total_selected,
        )

    async def update_tables_status_delta(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        activate: List[str] = None,
        deactivate: List[str] = None,
        current_user: User = None,
    ):
        """
        Update table is_active status using delta (lists of table names to activate/deactivate).
        More efficient than sending all tables.
        """
        from sqlalchemy import update, func
        from app.schemas.datasource_table_schema import DeltaUpdateTablesResponse
        
        # Verify data source exists
        result = await db.execute(
            select(DataSource).filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = result.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        activate = activate or []
        deactivate = deactivate or []
        
        activated_count = 0
        deactivated_count = 0
        
        # Activate tables
        if activate:
            activate_result = await db.execute(
                update(DataSourceTable)
                .where(
                    DataSourceTable.datasource_id == data_source_id,
                    DataSourceTable.name.in_(activate)
                )
                .values(is_active=True)
            )
            activated_count = activate_result.rowcount
        
        # Deactivate tables
        if deactivate:
            deactivate_result = await db.execute(
                update(DataSourceTable)
                .where(
                    DataSourceTable.datasource_id == data_source_id,
                    DataSourceTable.name.in_(deactivate)
                )
                .values(is_active=False)
            )
            deactivated_count = deactivate_result.rowcount
        
        await db.commit()
        
        # Get new total selected count
        selected_count_result = await db.execute(
            select(func.count(DataSourceTable.id)).where(
                DataSourceTable.datasource_id == data_source_id,
                DataSourceTable.is_active == True
            )
        )
        total_selected = selected_count_result.scalar() or 0
        
        return DeltaUpdateTablesResponse(
            activated_count=activated_count,
            deactivated_count=deactivated_count,
            total_selected=total_selected,
        )

    async def get_user_data_source_schema(self, db: AsyncSession, data_source: DataSource, user: User):
        """Fetch live schema with user creds, persist overlay rows, and return a user-scoped Table list."""
        client = await self.construct_client(db=db, data_source=data_source, current_user=user)
        fresh = client.get_schemas()
        if not fresh:
            return []

        # Normalize
        def normalize_columns(cols):
            return [{"name": (c.name if hasattr(c, "name") else c.get("name")), "dtype": (c.dtype if hasattr(c, "dtype") else c.get("dtype"))} for c in cols or []]

        normalized: dict[str, dict] = {}
        for t in fresh:
            if isinstance(t, dict):
                name = t.get("name")
                if not name:
                    continue
                normalized[name] = {
                    "columns": normalize_columns(t.get("columns", [])),
                    "pks": normalize_columns(t.get("pks", [])),
                    "fks": t.get("fks", []) or [],
                    "metadata_json": t.get("metadata_json"),
                }
            else:
                name = getattr(t, "name", None)
                if not name:
                    continue
                normalized[name] = {
                    "columns": normalize_columns(getattr(t, "columns", [])),
                    "pks": normalize_columns(getattr(t, "pks", [])),
                    "fks": getattr(t, "fks", []) or [],
                    "metadata_json": getattr(t, "metadata_json", None),
                }

        # Persist overlays
        await self._upsert_user_overlay(db=db, data_source=data_source, user=user, normalized=normalized)

        # Build Table models compatible with prompt formatters
        from app.ai.prompt_formatters import Table, TableColumn, ForeignKey as PromptForeignKey
        tables: list[Table] = []
        for name, payload in normalized.items():
            columns = [TableColumn(name=c["name"], dtype=c.get("dtype")) for c in (payload.get("columns") or [])]
            pks = [TableColumn(name=c["name"], dtype=c.get("dtype")) for c in (payload.get("pks") or [])]
            fks = []
            for fk in (payload.get("fks") or []):
                try:
                    fks.append(
                        PromptForeignKey(
                            column=TableColumn(name=fk["column"]["name"], dtype=fk["column"].get("dtype")),
                            references_name=fk["references_name"],
                            references_column=TableColumn(name=fk["references_column"]["name"], dtype=fk["references_column"].get("dtype")),
                        )
                    )
                except Exception:
                    continue
            tables.append(Table(name=name, columns=columns, pks=pks, fks=fks, metadata_json=payload.get("metadata_json")))

        return tables

    async def _upsert_user_overlay(self, db: AsyncSession, data_source: DataSource, user: User, normalized: dict[str, dict]):
        """Upsert per-user table/column overlay based on normalized schema."""
        now = datetime.now(timezone.utc)
        # Load canonical mapping to link if present
        existing_q = await db.execute(select(DataSourceTable).where(DataSourceTable.datasource_id == data_source.id))
        canonical_by_name = {row.name: row for row in existing_q.scalars().all()}

        for table_name, payload in normalized.items():
            # Upsert table overlay
            row_q = await db.execute(
                select(UserOverlayTable).where(
                    UserOverlayTable.data_source_id == data_source.id,
                    UserOverlayTable.user_id == user.id,
                    UserOverlayTable.table_name == table_name,
                )
            )
            t_row = row_q.scalar_one_or_none()
            if t_row is None:
                t_row = UserOverlayTable(
                    data_source_id=str(data_source.id),
                    user_id=str(user.id),
                    table_name=table_name,
                    data_source_table_id=str(canonical_by_name.get(table_name).id) if canonical_by_name.get(table_name) else None,
                    is_accessible=True,
                    status="accessible",
                    metadata_json=payload.get("metadata_json"),
                )
                db.add(t_row)
                await db.flush()
            else:
                t_row.metadata_json = payload.get("metadata_json")
                if t_row.data_source_table_id is None and canonical_by_name.get(table_name):
                    t_row.data_source_table_id = str(canonical_by_name.get(table_name).id)
                db.add(t_row)

            # Upsert column overlays
            existing_cols_q = await db.execute(select(UserOverlayColumn).where(UserOverlayColumn.user_data_source_table_id == t_row.id))
            existing_cols = {c.column_name: c for c in existing_cols_q.scalars().all()}
            for col in (payload.get("columns") or []):
                col_name = col.get("name")
                if not col_name:
                    continue
                c_row = existing_cols.get(col_name)
                if c_row is None:
                    c_row = UserOverlayColumn(
                        user_data_source_table_id=str(t_row.id),
                        column_name=col_name,
                        is_accessible=True,
                        is_masked=False,
                        data_type=col.get("dtype"),
                    )
                else:
                    c_row.data_type = col.get("dtype")
                db.add(c_row)

        await db.commit()
    
    async def update_table_status_in_schema(self, db: AsyncSession, data_source_id: str, tables: list[DataSourceTableSchema], organization: Organization):
        data_source = await self.get_data_source(db=db, data_source_id=data_source_id, organization=organization)
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        for table in tables:
            table_object = await db.execute(select(DataSourceTable).filter(DataSourceTable.datasource_id == data_source_id, DataSourceTable.name == table.name))
            table_object = table_object.scalar_one_or_none()
            if table_object:
                table_object.is_active = table.is_active
                await db.commit()
                await db.refresh(table_object)
        
        return data_source
    
    # Maximum tables to set as active when auto-selecting
    MAX_ACTIVE_TABLES = 500
    
    # Onboarding: auto-select a focused set of tables
    ONBOARDING_MAX_TABLES = 0

    async def save_or_update_tables(self, db: AsyncSession, data_source: DataSource, organization: Organization = None, should_set_active: bool = True, current_user: User | None = None, force_all_active: bool = False):
        """Diff-based upsert of datasource tables.
        - Insert new tables
        - Update changed tables
        - Deactivate missing tables (keep history)
        - If should_set_active and > ONBOARDING_MAX_TABLES, auto-select top tables via SQL
        - If force_all_active=True, bypass smart selection and activate all tables (for demos)
        """
        from sqlalchemy import text, update
        import json as json_module
        
        try:
            fresh_tables = await self.get_data_source_fresh_schema(db=db, data_source_id=data_source.id, organization=organization, current_user=current_user)
            if not fresh_tables:
                return

            # Map incoming by name
            def normalize_columns(cols):
                return [{"name": (c.name if hasattr(c, "name") else c.get("name")), "dtype": (c.dtype if hasattr(c, "dtype") else c.get("dtype"))} for c in cols or []]

            incoming = {}
            for t in fresh_tables:
                if isinstance(t, dict):
                    name = t.get("name")
                    if not name:
                        continue
                    incoming[name] = {
                        "columns": normalize_columns(t.get("columns", [])),
                        "pks": normalize_columns(t.get("pks", [])),
                        "fks": t.get("fks", []),
                        "metadata_json": t.get("metadata_json")
                    }
                else:
                    name = getattr(t, "name", None)
                    if not name:
                        continue
                    incoming[name] = {
                        "columns": normalize_columns(getattr(t, "columns", [])),
                        "pks": normalize_columns(getattr(t, "pks", [])),
                        "fks": getattr(t, "fks", []) or [],
                        "metadata_json": getattr(t, "metadata_json", None)
                    }

            total_tables = len(incoming)
            # Skip smart selection if force_all_active (e.g., demo data sources)
            needs_smart_selection = should_set_active and total_tables > self.ONBOARDING_MAX_TABLES and not force_all_active

            # Load existing table names only (not full objects for efficiency)
            existing_q = await db.execute(
                select(DataSourceTable.id, DataSourceTable.name)
                .where(DataSourceTable.datasource_id == data_source.id)
            )
            existing_names = {row.name: row.id for row in existing_q.fetchall()}

            # Prepare bulk insert for new tables
            new_tables = []
            for name, payload in incoming.items():
                if name not in existing_names:
                    new_tables.append({
                        "name": name,
                        "columns": json_module.dumps(payload["columns"]),
                        "pks": json_module.dumps(payload["pks"]),
                        "fks": json_module.dumps(payload["fks"]),
                        "datasource_id": str(data_source.id),
                        "is_active": False if needs_smart_selection else bool(should_set_active),
                        "metadata_json": json_module.dumps(payload.get("metadata_json")) if payload.get("metadata_json") else None,
                        "no_rows": 0,
                    })

            # Bulk insert new tables using ORM (database-agnostic)
            if new_tables:
                for table_data in new_tables:
                    db.add(DataSourceTable(
                        name=table_data["name"],
                        columns=json_module.loads(table_data["columns"]),
                        pks=json_module.loads(table_data["pks"]),
                        fks=json_module.loads(table_data["fks"]),
                        datasource_id=table_data["datasource_id"],  # Already a string
                        is_active=table_data["is_active"],
                        metadata_json=json_module.loads(table_data["metadata_json"]) if table_data["metadata_json"] else None,
                        no_rows=table_data["no_rows"],
                    ))
                await db.commit()

            # Update existing tables with new column data
            for name, payload in incoming.items():
                if name in existing_names:
                    table_id = existing_names[name]
                    await db.execute(
                        update(DataSourceTable)
                        .where(DataSourceTable.id == table_id)
                        .values(
                            columns=payload["columns"],
                            pks=payload["pks"],
                            fks=payload["fks"],
                            metadata_json=payload.get("metadata_json"),
                        )
                    )
            
            # Deactivate tables that no longer exist in fresh schema
            missing_tables = set(existing_names.keys()) - set(incoming.keys())
            if missing_tables:
                for table_name in missing_tables:
                    table_id = existing_names[table_name]
                    await db.execute(
                        update(DataSourceTable)
                        .where(DataSourceTable.id == table_id)
                        .values(is_active=False)
                    )
            
            await db.commit()

            # If smart selection needed, use SQL to select top tables (onboarding limit)
            if needs_smart_selection:
                await self._select_active_tables_sql(db, str(data_source.id), self.ONBOARDING_MAX_TABLES)

        except Exception as e:
            print(f"Error saving tables: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save database tables: {e}")

        # Return full schema including inactive for downstream context
        schemas = await data_source.get_schemas(db=db, include_inactive=True)
        return schemas

    async def _select_active_tables_sql(self, db: AsyncSession, datasource_id: str, max_active: int):
        """
        Select top tables based on:
        1. Schema distribution (spread across schemas proportionally)
        2. Column count (tables with more columns ranked higher)
        
        Uses efficient SQL with dialect-specific functions for PostgreSQL/SQLite.
        """
        from sqlalchemy import text
        
        # Detect database dialect
        bind = db.get_bind()
        dialect_name = bind.dialect.name if bind else "sqlite"
        is_postgres = dialect_name == "postgresql"
        
        # First, deactivate all tables for this datasource
        await db.execute(
            text("UPDATE datasource_tables SET is_active = :false_val WHERE datasource_id = :ds_id"),
            {"ds_id": datasource_id, "false_val": False}
        )
        
        # Build dialect-specific SQL for table selection
        if is_postgres:
            # PostgreSQL syntax
            json_schema_extract = "COALESCE(metadata_json->>'schema', CASE WHEN position('.' in name) > 0 THEN split_part(name, '.', 1) ELSE '__default__' END)"
            json_array_len = "COALESCE(jsonb_array_length(columns::jsonb), 0)"
            greatest_expr = "GREATEST(1, CAST(ROUND(1.0 * table_count / total_tables * :max_active) AS INTEGER))"
        else:
            # SQLite syntax (no GREATEST function, use MAX or CASE)
            json_schema_extract = "COALESCE(json_extract(metadata_json, '$.schema'), CASE WHEN instr(name, '.') > 0 THEN substr(name, 1, instr(name, '.') - 1) ELSE '__default__' END)"
            json_array_len = "COALESCE(json_array_length(columns), 0)"
            greatest_expr = "MAX(1, CAST(ROUND(1.0 * table_count / total_tables * :max_active) AS INTEGER))"
        
        # SQL to select top tables with proportional schema distribution
        # Uses window functions (standard SQL) to rank tables within each schema
        select_sql = text(f"""
            WITH table_stats AS (
                SELECT 
                    id,
                    name,
                    {json_schema_extract} as schema_name,
                    {json_array_len} as col_count
                FROM datasource_tables
                WHERE datasource_id = :ds_id
            ),
            schema_counts AS (
                SELECT 
                    schema_name,
                    COUNT(*) as table_count,
                    SUM(COUNT(*)) OVER () as total_tables
                FROM table_stats
                GROUP BY schema_name
            ),
            schema_allocations AS (
                SELECT 
                    schema_name,
                    -- Proportional allocation with minimum of 1
                    {greatest_expr} as allocation
                FROM schema_counts
            ),
            ranked_tables AS (
                SELECT 
                    t.id,
                    t.name,
                    t.schema_name,
                    t.col_count,
                    ROW_NUMBER() OVER (PARTITION BY t.schema_name ORDER BY t.col_count DESC, t.name) as rank_in_schema,
                    a.allocation
                FROM table_stats t
                JOIN schema_allocations a ON t.schema_name = a.schema_name
            ),
            selected_by_schema AS (
                SELECT id, col_count, rank_in_schema
                FROM ranked_tables
                WHERE rank_in_schema <= allocation
            )
            SELECT id FROM selected_by_schema
            ORDER BY col_count DESC
            LIMIT :max_active
        """)
        
        result = await db.execute(select_sql, {"ds_id": datasource_id, "max_active": max_active})
        selected_ids = [row[0] for row in result.fetchall()]
        
        # Activate selected tables
        if selected_ids:
            # Build placeholders for IN clause
            placeholders = ", ".join([f":id{i}" for i in range(len(selected_ids))])
            params = {f"id{i}": id_val for i, id_val in enumerate(selected_ids)}
            params["ds_id"] = datasource_id
            params["true_val"] = True
            
            await db.execute(
                text(f"UPDATE datasource_tables SET is_active = :true_val WHERE datasource_id = :ds_id AND id IN ({placeholders})"),
                params
            )
        
        await db.commit()
        
    
    async def refresh_data_source_schema(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        # Get the DataSource model instance with connections eagerly loaded
        result = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id)
        )
        data_source = result.scalar_one_or_none()

        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")

        # First, refresh ConnectionTable from the database (for all linked connections)
        if data_source.connections:
            from app.services.connection_service import ConnectionService
            connection_service = ConnectionService()
            logger.info(f"refresh_data_source_schema: Found {len(data_source.connections)} connections for data_source {data_source_id}")
            has_system_only_conn = False
            for conn in data_source.connections:
                logger.info(f"refresh_data_source_schema: Connection {conn.id} has auth_policy={conn.auth_policy}")
                if conn.auth_policy == "system_only":
                    has_system_only_conn = True
                    logger.info(f"refresh_data_source_schema: Calling refresh_schema for connection {conn.id}")
                    await connection_service.refresh_schema(db=db, connection=conn, current_user=current_user)
                    logger.info(f"refresh_data_source_schema: refresh_schema completed for connection {conn.id}")

            if has_system_only_conn:
                # Then sync from ConnectionTable to DataSourceTable
                # This ensures any new tables from the connection are added to the domain
                for conn in data_source.connections:
                    if conn.auth_policy == "system_only":
                        await self.sync_domain_tables_from_connection(
                            db, data_source, conn,
                            max_auto_select=None  # Don't auto-select, keep existing is_active state
                        )

                # Get updated schema
                schemas = await data_source.get_schemas(db=db, include_inactive=True)
                return schemas

        # Fallback to legacy behavior for user_required connections or if no connections
        # This uses data_source_service.resolve_credentials which has owner/admin fallback
        schemas = await self.save_or_update_tables(db=db, data_source=data_source, organization=organization, should_set_active=False, current_user=current_user)
        return schemas
    
    async def get_metadata_resources(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User = None):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        metadata_indexing_job = await db.execute(
            select(MetadataIndexingJob)
            .filter(
                MetadataIndexingJob.data_source_id == data_source_id,
                MetadataIndexingJob.status == IndexingJobStatus.COMPLETED.value,
                MetadataIndexingJob.is_active == True
            )
            .order_by(MetadataIndexingJob.created_at.desc())
            .limit(1)
        )
        metadata_indexing_job = metadata_indexing_job.scalar_one_or_none()
        
        if not metadata_indexing_job:
            raise HTTPException(status_code=404, detail="Metadata indexing job not found")
        
        resources = await db.execute(select(MetadataResource).filter(MetadataResource.data_source_id == data_source_id))
        resources = resources.scalars().all()
        
        # Import the schema
        from app.schemas.metadata_indexing_job_schema import MetadataIndexingJobSchema, JobStatus
        
        # Create a dict with all the job attributes
        job_data = {
            "id": metadata_indexing_job.id,
            "name": f"Indexing job for {data_source.name}",
            "description": f"Metadata indexing job for data source {data_source.name}",
            "job_type": "dbt",
            "status": JobStatus(metadata_indexing_job.status),
            "error_message": metadata_indexing_job.error_message,
            "resources_processed": metadata_indexing_job.processed_resources or 0,
            "resources_failed": 0,
            "started_at": metadata_indexing_job.started_at,
            "completed_at": metadata_indexing_job.completed_at,
            "data_source_id": metadata_indexing_job.data_source_id,
            "created_at": metadata_indexing_job.created_at,
            "updated_at": metadata_indexing_job.updated_at,
            "resources": [MetadataResourceSchema.from_orm(resource) for resource in resources],
            "config": {}
        }
        
        return MetadataIndexingJobSchema(**job_data)
    
    async def update_resources_status(self, db: AsyncSession, data_source_id: str, resources: list, organization: Organization, current_user: User = None):
        """Update the active status of DBT resources for a data source"""
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        for resource in resources:
            resource_object = await db.execute(
                select(MetadataResource).filter(
                    MetadataResource.id == resource.get('id'),
                    MetadataResource.data_source_id == data_source_id
                )
            )
            resource_object = resource_object.scalar_one_or_none()
            
            if resource_object:
                resource_object.is_active = resource.get('is_active', True)
                await db.commit()
                await db.refresh(resource_object)
        
        # Return updated resources
        resources = await db.execute(select(MetadataResource).filter(MetadataResource.data_source_id == data_source_id))
        resources = resources.scalars().all()

        # Get the metadata indexing job

        metadata_indexing_job = await self.get_metadata_resources(db=db, data_source_id=data_source_id, organization=organization, current_user=current_user)

        return metadata_indexing_job

    async def add_data_source_member(self, db: AsyncSession, data_source_id: str, member: DataSourceMembershipCreate, organization: Organization, current_user: User):
        """Add a user to data source membership"""
        # Get data source to verify it exists
        data_source = await self.get_data_source(db, data_source_id, organization)
        
        # Check if membership already exists
        existing = await db.execute(
            select(DataSourceMembership).filter(
                DataSourceMembership.data_source_id == data_source_id,
                DataSourceMembership.principal_type == member.principal_type,
                DataSourceMembership.principal_id == member.principal_id
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="User is already a member")
        
        # Create membership
        membership = DataSourceMembership(
            data_source_id=data_source_id,
            principal_type=member.principal_type,
            principal_id=member.principal_id,
            config=member.config
        )
        db.add(membership)
        await db.commit()
        await db.refresh(membership)
        return DataSourceMembershipSchema.from_orm(membership)

    async def remove_data_source_member(self, db: AsyncSession, data_source_id: str, user_id: str, organization: Organization, current_user: User):
        """Remove a user from data source membership"""
        # Get data source to verify it exists
        data_source = await self.get_data_source(db, data_source_id, organization)
        
        # Find and delete membership
        result = await db.execute(
            select(DataSourceMembership).filter(
                DataSourceMembership.data_source_id == data_source_id,
                DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                DataSourceMembership.principal_id == user_id
            )
        )
        membership = result.scalar_one_or_none()
        if not membership:
            raise HTTPException(status_code=404, detail="Membership not found")
        
        await db.delete(membership)
        await db.commit()
        return {"message": "Member removed successfully"}

    async def get_data_source_members(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        """Get all members of a data source"""
        # Get data source to verify it exists
        data_source = await self.get_data_source(db, data_source_id, organization, current_user)
        
        # Get data_source_memberships
        result = await db.execute(
            select(DataSourceMembership).filter(
                DataSourceMembership.data_source_id == data_source_id
            )
        )
        data_source_memberships = result.scalars().all()
        return [DataSourceMembershipSchema.from_orm(m) for m in data_source_memberships]

    async def _get_prompt_schema(self, db: AsyncSession, data_source: DataSource, organization: Organization, current_user: User | None) -> str:
        """Resolve a prompt-ready schema string for this data source.
        - For system_only: use canonical via DataSource.prompt_schema
        - For user_required with user: use per-user overlay tables and TableFormatter
        """
        # User-required path uses per-user overlays
        if getattr(data_source, "auth_policy", "system_only") == "user_required" and current_user is not None:
            tables = await self.get_user_data_source_schema(db=db, data_source=data_source, user=current_user)
            try:
                from app.ai.prompt_formatters import TableFormatter
                return TableFormatter(tables).table_str
            except Exception:
                # Fallback to no-stats canonical prompt schema
                return await data_source.prompt_schema(db=db, with_stats=False)
        # System path: canonical tables
        return await data_source.prompt_schema(db=db, with_stats=False)

    # ==================== Domain-Connection Architecture Methods ====================

    async def create_domain_with_connection(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        data_source_create: DataSourceCreate,
    ):
        """
        Create a DataSource (Domain) along with its Connection.
        This is the new architecture method that creates both in a single transaction.
        Maintains backward compatibility with existing create_data_source.
        """
        from app.services.connection_service import ConnectionService
        from app.models.connection import Connection
        from app.models.domain_connection import domain_connection
        
        connection_service = ConnectionService()
        data_source_dict = data_source_create.dict()
        
        # Extract connection-specific fields
        ds_type = data_source_dict.get("type")
        config = data_source_dict.pop("config", {})
        credentials = data_source_dict.pop("credentials", {})
        auth_policy = data_source_dict.get("auth_policy", "system_only")
        allowed_user_auth_modes = data_source_dict.pop("allowed_user_auth_modes", None)
        
        # Extract domain-specific fields
        name = data_source_dict.get("name")
        is_public = data_source_dict.get("is_public", False)
        member_user_ids = data_source_dict.pop("member_user_ids", [])
        generate_summary = data_source_dict.pop("generate_summary", False)
        generate_conversation_starters = data_source_dict.pop("generate_conversation_starters", False)
        generate_ai_rules = data_source_dict.pop("generate_ai_rules", False)
        use_llm_sync = data_source_dict.pop("use_llm_sync", False)
        
        # Create the Connection first
        connection = await connection_service.create_connection(
            db=db,
            organization=organization,
            current_user=current_user,
            name=name,
            type=ds_type,
            config=config,
            credentials=credentials,
            auth_policy=auth_policy,
            allowed_user_auth_modes=allowed_user_auth_modes,
        )
        
        # Create the DataSource (Domain) - connection fields are now on Connection model
        new_data_source = DataSource(
            name=name,
            organization_id=organization.id,
            is_public=is_public,
            use_llm_sync=use_llm_sync,
            owner_user_id=current_user.id,
        )
        
        db.add(new_data_source)
        await db.flush()
        
        # Link domain to connection via junction table
        await db.execute(
            domain_connection.insert().values(
                data_source_id=new_data_source.id,
                connection_id=connection.id
            )
        )
        
        await db.commit()
        await db.refresh(new_data_source)
        
        # Create memberships
        await self._create_memberships(db, new_data_source, [current_user.id])
        if member_user_ids and not is_public:
            additional_user_ids = [uid for uid in member_user_ids if uid != current_user.id]
            if additional_user_ids:
                await self._create_memberships(db, new_data_source, additional_user_ids)
        
        # Sync domain tables from connection tables (onboarding: auto-select up to 20)
        await self.sync_domain_tables_from_connection(
            db, new_data_source, connection, 
            max_auto_select=self.ONBOARDING_MAX_TABLES
        )
        
        # Generate AI content if requested
        if auth_policy == "system_only":
            if generate_summary:
                response = await self.generate_data_source_items(db=db, item="summary", data_source_id=new_data_source.id, organization=organization, current_user=current_user)
                new_data_source.description = response.get("summary")
            if generate_conversation_starters:
                response = await self.generate_data_source_items(db=db, item="conversation_starters", data_source_id=new_data_source.id, organization=organization, current_user=current_user)
                new_data_source.conversation_starters = response.get("conversation_starters")
            await db.commit()
            await db.refresh(new_data_source)
        
        # Reload with relationships
        stmt = (
            select(DataSource)
            .options(
                selectinload(DataSource.data_source_memberships),
                selectinload(DataSource.connections)
            )
            .where(DataSource.id == new_data_source.id)
        )
        result = await db.execute(stmt)
        return result.scalar_one()

    async def add_connection_to_domain(
        self,
        db: AsyncSession,
        data_source_id: str,
        connection_id: str,
        organization: Organization,
        current_user: User,
        sync_tables: bool = True,
    ):
        """
        Add a connection to an existing domain (M:N relationship).
        """
        from app.models.connection import Connection
        from app.models.domain_connection import domain_connection
        
        # Verify domain exists
        data_source = await db.execute(
            select(DataSource).filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = data_source.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Domain not found")
        
        # Verify connection exists
        connection = await db.execute(
            select(Connection).filter(
                Connection.id == connection_id,
                Connection.organization_id == organization.id
            )
        )
        connection = connection.scalar_one_or_none()
        if not connection:
            raise HTTPException(status_code=404, detail="Connection not found")
        
        # Check if already linked
        existing = await db.execute(
            domain_connection.select().where(
                domain_connection.c.data_source_id == data_source_id,
                domain_connection.c.connection_id == connection_id
            )
        )
        if existing.first():
            raise HTTPException(status_code=400, detail="Connection already linked to this domain")
        
        # Create link
        await db.execute(
            domain_connection.insert().values(
                data_source_id=data_source_id,
                connection_id=connection_id
            )
        )
        await db.commit()
        
        # Sync domain tables from this connection (no auto-select for existing domains)
        if sync_tables:
            await self.sync_domain_tables_from_connection(
                db, data_source, connection,
                max_auto_select=None  # User must manually select tables
            )
        
        return {"message": "Connection added to domain"}

    async def remove_connection_from_domain(
        self,
        db: AsyncSession,
        data_source_id: str,
        connection_id: str,
        organization: Organization,
        current_user: User,
    ):
        """
        Remove a connection from a domain.
        """
        from app.models.domain_connection import domain_connection
        
        # Verify domain exists
        data_source = await db.execute(
            select(DataSource).options(selectinload(DataSource.connections)).filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = data_source.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Domain not found")
        
        # Check if this is the last connection
        if len(data_source.connections) <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the last connection from a domain")
        
        # Remove link
        await db.execute(
            domain_connection.delete().where(
                domain_connection.c.data_source_id == data_source_id,
                domain_connection.c.connection_id == connection_id
            )
        )
        
        # Remove domain tables that reference this connection's tables
        from app.models.connection_table import ConnectionTable
        await db.execute(
            delete(DataSourceTable).where(
                DataSourceTable.datasource_id == data_source_id,
                DataSourceTable.connection_table_id.in_(
                    select(ConnectionTable.id).where(ConnectionTable.connection_id == connection_id)
                )
            )
        )
        
        await db.commit()
        return {"message": "Connection removed from domain"}

    async def sync_domain_tables_from_connection(
        self,
        db: AsyncSession,
        data_source: DataSource,
        connection,
        max_auto_select: int | None = None,
    ):
        """
        Create DataSourceTable (DomainTable) entries from ConnectionTable entries.
        Links domain tables to connection tables for schema access.
        
        Args:
            max_auto_select: Maximum tables to auto-select.
                - None: No auto-selection, all tables start inactive (for new domains from existing connections)
                - int: Auto-select up to this many tables (for onboarding, use ONBOARDING_MAX_TABLES=20)
        """
        from app.models.connection_table import ConnectionTable

        # Get connection tables - ensure connection_id is string
        connection_id_str = str(connection.id)
        conn_tables = await db.execute(
            select(ConnectionTable).filter(ConnectionTable.connection_id == connection_id_str)
        )
        conn_tables = conn_tables.scalars().all()

        logger.info(f"sync_domain_tables_from_connection: Found {len(conn_tables)} ConnectionTable records for connection {connection_id_str}")

        if not conn_tables:
            logger.warning(f"sync_domain_tables_from_connection: No ConnectionTable records found, cannot sync")
            return
        
        # Get existing domain tables
        existing = await db.execute(
            select(DataSourceTable).filter(DataSourceTable.datasource_id == data_source.id)
        )
        existing_by_name = {t.name: t for t in existing.scalars().all()}
        
        total_tables = len(conn_tables)

        # Determine initial activation:
        # - If max_auto_select is None: all tables start inactive (user must select)
        # - If max_auto_select is set and total <= limit: activate all
        # - If max_auto_select is set and total > limit: start inactive, then smart-select
        if max_auto_select is None:
            should_activate = False
            needs_smart_selection = False
        else:
            should_activate = total_tables <= max_auto_select
            needs_smart_selection = total_tables > max_auto_select

        for conn_table in conn_tables:
            if conn_table.name in existing_by_name:
                # Update existing - link to connection table and refresh schema data
                domain_table = existing_by_name[conn_table.name]
                domain_table.connection_table_id = conn_table.id
                # Update legacy fields with latest schema (preserves is_active)
                domain_table.columns = conn_table.columns
                domain_table.pks = conn_table.pks
                domain_table.fks = conn_table.fks
                domain_table.no_rows = conn_table.no_rows
                domain_table.metadata_json = conn_table.metadata_json
            else:
                # Create new domain table linked to connection table
                domain_table = DataSourceTable(
                    name=conn_table.name,
                    datasource_id=data_source.id,
                    connection_table_id=conn_table.id,
                    is_active=should_activate,
                    # Copy legacy fields for backward compatibility
                    columns=conn_table.columns,
                    pks=conn_table.pks,
                    fks=conn_table.fks,
                    no_rows=conn_table.no_rows,
                    metadata_json=conn_table.metadata_json,
                    centrality_score=conn_table.centrality_score,
                    richness=conn_table.richness,
                    degree_in=conn_table.degree_in,
                    degree_out=conn_table.degree_out,
                    entity_like=conn_table.entity_like,
                    metrics_computed_at=conn_table.metrics_computed_at,
                )
                db.add(domain_table)

        # Deactivate domain tables that no longer exist in the connection
        # (table was deleted from the database)
        # IMPORTANT: Only check tables that belong to THIS connection, not all domain tables
        conn_table_names = {t.name for t in conn_tables}

        # Get domain tables that are linked to THIS connection (via ConnectionTable)
        existing_for_this_conn = await db.execute(
            select(DataSourceTable)
            .join(ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id)
            .where(
                DataSourceTable.datasource_id == data_source.id,
                ConnectionTable.connection_id == connection_id_str
            )
        )
        existing_for_this_conn = {t.name: t for t in existing_for_this_conn.scalars().all()}

        missing_tables = set(existing_for_this_conn.keys()) - conn_table_names
        if missing_tables:
            from sqlalchemy import update
            for table_name in missing_tables:
                domain_table = existing_for_this_conn[table_name]
                await db.execute(
                    update(DataSourceTable)
                    .where(DataSourceTable.id == domain_table.id)
                    .values(is_active=False)
                )

        await db.commit()

        # If too many tables for auto-select, use smart selection algorithm
        if needs_smart_selection and max_auto_select:
            await self._select_active_tables_sql(db, str(data_source.id), max_auto_select)

    async def get_domain_connections(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
    ):
        """Get all connections linked to a domain."""
        data_source = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = data_source.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Domain not found")
        
        return data_source.connections

