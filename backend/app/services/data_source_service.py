import importlib
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource, DATA_SOURCE_DETAILS
from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
from app.models.metadata_resource import MetadataResource
from app.models.metadata_indexing_job import MetadataIndexingJob, IndexingJobStatus

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.schemas.data_source_schema import (
    DataSourceCreate, DataSourceBase, DataSourceSchema, DataSourceUpdate,
    DataSourceMembershipSchema, DataSourceMembershipCreate
)
from app.schemas.metadata_resource_schema import MetadataResourceSchema

from pydantic import BaseModel
from app.ai.agents.data_source.data_source import DataSourceAgent
from fastapi import HTTPException

import uuid
from uuid import UUID
import json

from sqlalchemy import insert, delete, or_, and_
from app.schemas.datasource_table_schema import DataSourceTableSchema
from app.models.datasource_table import DataSourceTable  # Add this import at the top of the file

from typing import List
from sqlalchemy.orm import selectinload

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
        member_user_ids = data_source_dict.pop("member_user_ids", [])
        
        # Create base data source dict
        ds_create_dict = {
            "name": data_source_dict["name"],
            "type": data_source_dict["type"],
            "config": json.dumps(config),
            "organization_id": organization.id,
            "is_public": is_public,
            "owner_user_id": current_user.id
        }
        
        # Create the data source instance
        new_data_source = DataSource(**ds_create_dict)
        
        # Encrypt and store credentials
        new_data_source.encrypt_credentials(credentials)
        
        db.add(new_data_source)
        await db.commit()
        await db.refresh(new_data_source)
        
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

        schema = await data_source.get_schemas(db=db, include_inactive=False)

        data_source_agent = DataSourceAgent(data_source=data_source, model=model, schema=schema)
        response = {}
        if item == "summary":
            response["summary"] = data_source_agent.generate_summary()
        elif item == "conversation_starters":
            response["conversation_starters"] = data_source_agent.generate_conversation_starters()
        elif item == "description":
            response["description"] = data_source_agent.generate_description()

        return response

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
        
        return DataSourceSchema.from_orm(data_source)

    async def get_available_data_sources(self, db: AsyncSession, organization: Organization):
        return [ds for ds in DATA_SOURCE_DETAILS if ds["status"] == "active"]
    
    async def get_data_sources(self, db: AsyncSession, current_user: User, organization: Organization) -> List[DataSourceSchema]:
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
        return [DataSourceSchema.from_orm(d) for d in data_sources]

    async def get_active_data_sources(self, db: AsyncSession, organization: Organization, current_user: User = None):
        """Get all active data sources for an organization that the user has access to"""
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
        return [DataSourceSchema.from_orm(d) for d in data_sources]
    
    async def get_data_source_fields(self, db: AsyncSession, data_source_type: str, organization: Organization, current_user: User):
        ds = next((ds for ds in DATA_SOURCE_DETAILS if ds["type"] == data_source_type), None)
        if not ds:
            raise ValueError(f"Unknown data source type: {data_source_type}")

        schema_module = importlib.import_module("app.schemas.data_source_schema")
        
        # Get both config and credentials schemas
        config_schema_name = ds.get("config")
        credentials_schema_name = config_schema_name.replace("Config", "Credentials")
        
        try:
            config_schema = getattr(schema_module, config_schema_name)
            credentials_schema = getattr(schema_module, credentials_schema_name)
            
            # Extract fields from both schemas
            config_fields = self._extract_fields_from_schema(schema=config_schema)
            credentials_fields = self._extract_fields_from_schema(schema=credentials_schema)
            
            # Return both sets of fields
            return {
                "config": config_fields,
                "credentials": credentials_fields,
                "type": data_source_type,
                "title": ds.get("title"),
                "description": ds.get("description")
            }
        except AttributeError as e:
            raise ValueError(f"Schema not found for {data_source_type}: {str(e)}")
    
    async def delete_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
            
        await self.delete_data_source_tables(db=db, data_source_id=data_source_id, organization=organization, current_user=current_user)
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
            client = data_source.get_client()
            # Test the connection
            connection_status = client.test_connection()

            if connection_status["success"] == False:
                # set data_source active=False
                data_source.is_active = False
                await db.commit()
                await db.refresh(data_source)
            else:
                if data_source.is_active == False:
                    data_source.is_active = True
                    await db.commit()
                    await db.refresh(data_source)

        except Exception as e:
            # set data_source active=False

            data_source.is_active = False
            await db.commit()
            await db.refresh(data_source)

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
            client = self._resolve_client_by_type(data_source_type=data_source_type, config=config, credentials=credentials)

            # Test the connection
            connection_status = client.test_connection()
        except Exception as e:
            connection_status = {
                "success": False,
                "message": str(e)
            }

        return connection_status

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
            

            
        client = data_source.get_client()
        try:
            schema = client.get_schemas()
            if not schema:
                raise HTTPException(status_code=500, detail="No schema returned from data source")
            return schema
        except Exception as e:
            print(f"Error getting data source schema: {e}")
            raise HTTPException(status_code=500, detail=f"Error getting data source schema: {e}")
    
    async def get_data_source_schema(self, db: AsyncSession, data_source_id: str, include_inactive: bool = False, organization: Organization = None, current_user: User = None):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
            


        schemas = await data_source.get_schemas(db=db, include_inactive=include_inactive)

        return schemas
    
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
    
    async def save_or_update_tables(self, db: AsyncSession, data_source: DataSource, organization: Organization = None, should_set_active: bool = True):
        """Diff-based upsert of datasource tables.
        - Insert new tables
        - Update changed tables
        - Deactivate missing tables (keep history)
        """
        try:
            fresh_tables = await self.get_data_source_fresh_schema(db=db, data_source_id=data_source.id, organization=organization)
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
                    }
                else:
                    name = getattr(t, "name", None)
                    if not name:
                        continue
                    incoming[name] = {
                        "columns": normalize_columns(getattr(t, "columns", [])),
                        "pks": normalize_columns(getattr(t, "pks", [])),
                        "fks": getattr(t, "fks", []) or [],
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
                    if row.columns != payload["columns"] or row.pks != payload["pks"] or row.fks != payload["fks"]:
                        row.columns = payload["columns"]
                        row.pks = payload["pks"]
                        row.fks = payload["fks"]
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

