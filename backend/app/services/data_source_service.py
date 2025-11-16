import importlib
from app.models.user import User
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
    DataSourceListItemSchema,
)
from app.schemas.metadata_resource_schema import MetadataResourceSchema

from pydantic import BaseModel
from app.ai.agents.data_source.data_source import DataSourceAgent
from fastapi import HTTPException

import uuid
from uuid import UUID
import json
from datetime import datetime, timezone

from sqlalchemy import insert, delete, or_, and_
from sqlalchemy.exc import IntegrityError
from app.schemas.datasource_table_schema import DataSourceTableSchema
from app.models.datasource_table import DataSourceTable  # Add this import at the top of the file
from app.models.user_data_source_overlay import UserDataSourceTable as UserOverlayTable, UserDataSourceColumn as UserOverlayColumn
from app.models.report_data_source_association import report_data_source_association
from app.models.instruction import instruction_data_source_association
from app.models.entity import entity_data_source_association

from typing import List
from sqlalchemy.orm import selectinload
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import InstructionCreate
from app.core.telemetry import telemetry

class DataSourceService:

    def __init__(self):
        pass

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
        
        # Extract special flags
        generate_summary = data_source_dict.pop("generate_summary")
        generate_conversation_starters = data_source_dict.pop("generate_conversation_starters")
        generate_ai_rules = data_source_dict.pop("generate_ai_rules")
        
        # Extract credentials, config, and membership info
        credentials = data_source_dict.pop("credentials")
        config = data_source_dict.pop("config")
        is_public = data_source_dict.pop("is_public", False)
        use_llm_sync = data_source_dict.pop("use_llm_sync", False)
        member_user_ids = data_source_dict.pop("member_user_ids", [])
        auth_policy = data_source_dict.get("auth_policy", "system_only")
        
        # Create base data source dict
        ds_create_dict = {
            "name": data_source_dict["name"],
            "type": data_source_dict["type"],
            "config": json.dumps(config),
            "organization_id": organization.id,
            "is_public": is_public,
            "use_llm_sync": use_llm_sync,
            "auth_policy": auth_policy,
            "owner_user_id": current_user.id
        }
        
        # Create the data source instance
        new_data_source = DataSource(**ds_create_dict)
        
        # Encrypt and store credentials
        new_data_source.encrypt_credentials(credentials)
        
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
                    "type": new_data_source.type,
                    "is_public": bool(is_public),
                    "auth_policy": auth_policy,
                    "use_llm_sync": bool(use_llm_sync),
                },
                user_id=current_user.id,
                org_id=organization.id,
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

        # Test connection and generate items...
        connection = await self.test_data_source_connection(db=db, data_source_id=new_data_source.id, organization=organization, current_user=current_user)
        if connection["success"]:
            if getattr(new_data_source, "auth_policy", "system_only") == "system_only":
                await self.save_or_update_tables(db=db, data_source=new_data_source, organization=organization, should_set_active=True)

            if generate_summary:
                response = await self.generate_data_source_items(db=db, item="summary", data_source_id=new_data_source.id, organization=organization, current_user=current_user)
                new_data_source.description = response["summary"]
            if generate_conversation_starters:
                response = await self.generate_data_source_items(db=db, item="conversation_starters", data_source_id=new_data_source.id, organization=organization, current_user=current_user)
                new_data_source.conversation_starters = response["conversation_starters"]
            if generate_ai_rules:
                pass
            await db.commit()
            await db.refresh(new_data_source)

        # Reload the data source with memberships to avoid serialization issues
        stmt = (
            select(DataSource)
            .options(selectinload(DataSource.data_source_memberships))
            .where(DataSource.id == new_data_source.id)
        )
        result = await db.execute(stmt)
        final_data_source = result.scalar_one()
        
        return final_data_source

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
            async for draft in suggest_instructions.onboarding_suggestions(context_view=view):
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
                    continue
            if created_instruction_payloads:
                result["instructions"] = created_instruction_payloads
        except Exception as e:
            pass

        return result

    async def get_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User = None) -> DataSourceSchema:
        query = (
            select(DataSource)
            .options(
                selectinload(DataSource.git_repository),
                selectinload(DataSource.data_source_memberships)
            )
            .filter(DataSource.id == data_source_id)
            .filter(DataSource.organization_id == organization.id)
        )
        result = await db.execute(query)
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")

        # Persist connectivity for system-only sources when viewing details
        # This ensures UI status aligns with org-level is_active
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
                        selectinload(DataSource.data_source_memberships)
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

        schema = DataSourceSchema.from_orm(data_source)
        # Attach user_status via user creds service when a user context exists
        try:
            if current_user:
                from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
                u_svc = UserDataSourceCredentialsService()
                schema.user_status = await u_svc.build_user_status(db=db, data_source=data_source, user=current_user, live_test=True)
        except Exception:
            pass
        
        return schema


    async def get_available_data_sources(self, db: AsyncSession, organization: Organization):
        return list_available_data_sources()
    
    async def get_data_sources(self, db: AsyncSession, current_user: User, organization: Organization) -> List[DataSourceListItemSchema]:
        # Query for data sources the user has access to
        query = (
            select(DataSource)
            .options(
                selectinload(DataSource.git_repository),
                selectinload(DataSource.data_source_memberships)
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
        # Build list with user_status (no live test for list to keep it fast)
        from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
        u_svc = UserDataSourceCredentialsService()
        schemas: list[DataSourceListItemSchema] = []
        for d in data_sources:
            s = DataSourceListItemSchema(
                id=str(d.id),
                name=d.name,
                type=d.type,
                auth_policy=d.auth_policy,
                description=getattr(d, "description", None),
                created_at=d.created_at,
                status=("active" if bool(d.is_active) else "inactive"),
            )
            try:
                s.user_status = await u_svc.build_user_status(db=db, data_source=d, user=current_user, live_test=False)
            except Exception:
                pass
            schemas.append(s)
        return schemas

    async def get_active_data_sources(self, db: AsyncSession, organization: Organization, current_user: User = None) -> List[DataSourceListItemSchema]:
        """Get all active data sources for an organization that the user has access to, compact list shape"""
        # Build base query for active data sources
        stmt = (
            select(DataSource)
            .options(selectinload(DataSource.data_source_memberships))
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
        from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
        u_svc = UserDataSourceCredentialsService()
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
            s = DataSourceListItemSchema(
                id=str(d.id),
                name=d.name,
                type=d.type,
                auth_policy=d.auth_policy,
                conversation_starters=getattr(d, "conversation_starters", None),
                description=getattr(d, "description", None),
                created_at=d.created_at,
                status=("active" if bool(d.is_active) else "inactive"),
            )
            try:
                if current_user:
                    s.user_status = await u_svc.build_user_status(db=db, data_source=d, user=current_user, live_test=False)
            except Exception:
                pass
            # Exclude user_required data sources lacking user credentials,
            # unless the user has permission to update data sources (admin/editor)
            if getattr(d, "auth_policy", "system_only") == "user_required" and current_user:
                try:
                    has_user_creds = getattr(s.user_status, "has_user_credentials", False)
                except Exception:
                    has_user_creds = False
                if not has_user_creds and not has_update_perm:
                    continue
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

        # 2) Delete association-table links to avoid FK blockers
        await db.execute(
            delete(report_data_source_association).where(
                report_data_source_association.c.data_source_id == data_source_id
            )
        )
        await db.execute(
            delete(instruction_data_source_association).where(
                instruction_data_source_association.c.data_source_id == data_source_id
            )
        )
        await db.execute(
            delete(entity_data_source_association).where(
                entity_data_source_association.c.data_source_id == data_source_id
            )
        )

        # 3) Remove direct child rows managed by ORM on update but not guaranteed by DB cascades
        await db.execute(
            delete(DataSourceMembership).where(DataSourceMembership.data_source_id == data_source_id)
        )
        await db.execute(
            delete(UserDataSourceCredentials).where(UserDataSourceCredentials.data_source_id == data_source_id)
        )

        # 4) Delete dependent metadata resources first (they FK both data source and jobs)
        resources_q = await db.execute(
            select(MetadataResource).where(MetadataResource.data_source_id == data_source_id)
        )
        for resource in resources_q.scalars().all():
            await db.delete(resource)

        # 5) Delete metadata indexing jobs for this data source
        jobs_q = await db.execute(
            select(MetadataIndexingJob).where(MetadataIndexingJob.data_source_id == data_source_id)
        )
        for job in jobs_q.scalars().all():
            await db.delete(job)

        # 6) Delete any linked git repository for this data source
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

        # 7) Delete schema tables associated with this data source (commits internally)
        await self.delete_data_source_tables(db=db, data_source_id=data_source_id, organization=organization, current_user=current_user)

        # 8) Finally delete the data source
        await db.delete(data_source)
        await db.commit()
        return {"message": "Data source deleted successfully"}
    
    async def delete_data_source_tables(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        result = await db.execute(select(DataSourceTable).filter(DataSourceTable.datasource_id == data_source_id))
        tables = result.scalars().all()
        for table in tables:
            await db.delete(table)
        await db.commit()
        return {"message": "Data source tables deleted successfully"}
    
    async def test_data_source_connection(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        try:
            # Find the data source
            result = await db.execute(
                select(DataSource).filter(
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

            # Reflect connectivity on org-wide flag only for system creds
            if getattr(data_source, "auth_policy", "system_only") == "system_only":
                if not success:
                    data_source.is_active = False
                    await db.commit()
                    await db.refresh(data_source)
                else:
                    if data_source.is_active == False:
                        data_source.is_active = True
                        await db.commit()
                        await db.refresh(data_source)

        except Exception as e:
            # For system creds, mark DS inactive; for user creds, don't flip org state
            try:
                if 'data_source' in locals() and getattr(data_source, "auth_policy", "system_only") == "system_only":
                    data_source.is_active = False
                    await db.commit()
                    await db.refresh(data_source)
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

            # Test the connection
            connection_status = client.test_connection()
        except Exception as e:
            connection_status = {
                "success": False,
                "message": str(e)
            }

        return connection_status

    async def resolve_credentials(self, db: AsyncSession, data_source: DataSource, current_user: User | None) -> dict:
        # system_only → use stored system credentials
        if getattr(data_source, "auth_policy", "system_only") == "system_only":
            try:
                return data_source.decrypt_credentials() or {}
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
            if (is_owner or has_update_perm) and getattr(data_source, "credentials", None):
                try:
                    return data_source.decrypt_credentials() or {}
                except Exception:
                    pass
            raise HTTPException(status_code=403, detail="User credentials required for this data source")
        return row.decrypt_credentials() or {}

    async def construct_client(self, db: AsyncSession, data_source: DataSource, current_user: User | None):
        # Resolve client class from registry (no model dependency)
        ClientClass = resolve_client_class(data_source.type)
        # Merge config and creds
        config = json.loads(data_source.config) if isinstance(data_source.config, str) else (data_source.config or {})
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
            .options(selectinload(DataSource.data_source_memberships))
            .filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id)
        )
        data_source_db = result.scalar_one_or_none()
        
        if not data_source_db:
            raise HTTPException(status_code=404, detail="Data source not found")



        # Extract the update data
        update_data = data_source.dict(exclude_unset=True)
        
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
        
        # Handle config updates
        if 'config' in update_data:
            config = update_data.pop('config')
            data_source_db.config = json.dumps(config)
        
        # Handle credentials updates
        if 'credentials' in update_data:
            credentials = update_data.pop('credentials')
            # Only update credentials if none of its values are None
            if credentials and not any(value is None for value in credentials.values()):
                data_source_db.encrypt_credentials(credentials)
        
        # Update remaining fields
        for field, value in update_data.items():
            if value is not None:
                setattr(data_source_db, field, value)
        
        try:
            await db.commit()
            
            # Reload the data source with memberships to avoid serialization issues
            stmt = (
                select(DataSource)
                .options(selectinload(DataSource.data_source_memberships))
                .where(DataSource.id == data_source_db.id)
            )
            result = await db.execute(stmt)
            final_data_source = result.scalar_one()
            
            return final_data_source
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
        logger.info(f"Extracted schema: {main_model_schema}")

        return main_model_schema

    async def get_data_source_fresh_schema(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User = None):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
            

        client = await self.construct_client(db=db, data_source=data_source, current_user=current_user)
        try:
            schema = client.get_schemas()
            if not schema:
                raise HTTPException(status_code=500, detail="No schema returned from data source")
            return schema
        except Exception as e:
            print(f"Error getting data source schema: {e}")
            raise HTTPException(status_code=500, detail=f"Error getting data source schema: {e}")
    
    async def get_data_source_schema(self, db: AsyncSession, data_source_id: str, include_inactive: bool = False, organization: Organization = None, current_user: User = None, with_stats: bool = False):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
            
        # For user_required policy, prefer the user's live schema view and upsert overlay
        if getattr(data_source, "auth_policy", "system_only") == "user_required" and current_user is not None:
            try:
                return await self.get_user_data_source_schema(db=db, data_source=data_source, user=current_user)
            except Exception:
                # Fallback to canonical schema if user overlay fetch fails
                pass

        schemas = await data_source.get_schemas(db=db, include_inactive=include_inactive, with_stats=with_stats)

        return schemas

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
    
    async def save_or_update_tables(self, db: AsyncSession, data_source: DataSource, organization: Organization = None, should_set_active: bool = True, current_user: User | None = None):
        """Diff-based upsert of datasource tables.
        - Insert new tables
        - Update changed tables
        - Deactivate missing tables (keep history)
        """
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

            # Load existing
            existing_q = await db.execute(select(DataSourceTable).where(DataSourceTable.datasource_id == data_source.id))
            existing_rows = {row.name: row for row in existing_q.scalars().all()}

            # Upserts
            changed = False
            for name, payload in incoming.items():
                if name in existing_rows:
                    row = existing_rows[name]
                    # Detect diffs (shallow compare)
                    if row.columns != payload["columns"] or row.pks != payload["pks"] or row.fks != payload["fks"] or row.metadata_json != payload.get("metadata_json"):
                        row.columns = payload["columns"]
                        row.pks = payload["pks"]
                        row.fks = payload["fks"]
                        row.metadata_json = payload.get("metadata_json")
                        row.is_active = True
                        changed = True
                else:
                    db.add(DataSourceTable(
                        name=name,
                        columns=payload["columns"],
                        pks=payload["pks"],
                        fks=payload["fks"],
                        datasource_id=data_source.id,
                        is_active=should_set_active,
                        metadata_json=payload.get("metadata_json"),
                    ))
                    changed = True

            # Deactivate missing
            missing = set(existing_rows.keys()) - set(incoming.keys())
            for name in missing:
                row = existing_rows[name]
                if row.is_active:
                    row.is_active = False
                    changed = True

            if changed:
                await db.commit()

        except Exception as e:
            print(f"Error saving tables: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save database tables: {e}")

        # Return full schema including inactive for downstream context
        schemas = await data_source.get_schemas(db=db, include_inactive=True)
        return schemas
        
    
    async def refresh_data_source_schema(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        # Get the DataSource model instance instead of schema
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        schemas = await self.save_or_update_tables(db=db, data_source=data_source, organization=organization, should_set_active=False)
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

