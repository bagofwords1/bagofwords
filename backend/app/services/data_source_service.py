import importlib
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource, DATA_SOURCE_DETAILS
from app.models.metadata_resource import MetadataResource
from app.models.metadata_indexing_job import MetadataIndexingJob, IndexingJobStatus

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.schemas.data_source_schema import DataSourceCreate, DataSourceBase, DataSourceSchema, DataSourceUpdate
from app.schemas.metadata_resource_schema import MetadataResourceSchema

from pydantic import BaseModel
from app.ai.agents.data_source.data_source import DataSourceAgent
from fastapi import HTTPException

import uuid
from uuid import UUID
import json

from sqlalchemy import insert, delete
from app.schemas.datasource_table_schema import DataSourceTableSchema
from app.models.datasource_table import DataSourceTable  # Add this import at the top of the file

from typing import List
from sqlalchemy.orm import selectinload

class DataSourceService:

    def __init__(self):
        pass

    async def create_data_source(self, db: AsyncSession, organization: Organization, current_user: User, data_source: DataSourceCreate):
        # Convert Pydantic model to dict
        data_source_dict = data_source.dict()

        if data_source_dict['name'] == '':
            raise HTTPException(status_code=400, detail="Data source name is required")
        
        # Extract special flags
        generate_summary = data_source_dict.pop("generate_summary")
        generate_conversation_starters = data_source_dict.pop("generate_conversation_starters")
        generate_ai_rules = data_source_dict.pop("generate_ai_rules")
        
        # Extract credentials and config
        credentials = data_source_dict.pop("credentials")
        config = data_source_dict.pop("config")
        
        # Create base data source dict
        ds_create_dict = {
            "name": data_source_dict["name"],
            "type": data_source_dict["type"],
            "config": json.dumps(config),
            "organization_id": organization.id
        }
        
        # Create the data source instance
        new_data_source = DataSource(**ds_create_dict)
        
        # Encrypt and store credentials
        new_data_source.encrypt_credentials(credentials)
        
        db.add(new_data_source)
        await db.commit()
        await db.refresh(new_data_source)

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

        return new_data_source

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

    async def get_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization) -> DataSourceSchema:
        query = (
            select(DataSource)
            .options(selectinload(DataSource.git_repository))  # Explicitly load the relationship
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
        ds = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.git_repository))  # Explicitly load the relationship
            .filter(DataSource.organization_id == organization.id)
        )
        ds = ds.scalars().all()
        return [DataSourceSchema.from_orm(d) for d in ds]

    async def get_active_data_sources(self, db: AsyncSession, organization: Organization):
        """Get all active data sources for an organization"""
        stmt = select(DataSource).where(
            DataSource.organization_id == organization.id,
            DataSource.is_active == True
        )
        result = await db.execute(stmt)
        ds = result.scalars().all()
        
        # Create schemas without trying to load git_repository
        data_sources = []
        for d in ds:
            # Create a dict with all the fields except git_repository
            data_source_dict = {
                "id": d.id,
                "name": d.name,
                "type": d.type,
                "config": d.config,
                "organization_id": d.organization_id,
                "created_at": d.created_at,
                "updated_at": d.updated_at,
                "context": d.context,
                "description": d.description,
                "summary": d.summary,
                "conversation_starters": d.conversation_starters,
                "is_active": d.is_active,
                "git_repository": None  # Set to None initially
            }
            
            # Create the schema from the dict
            data_source_schema = DataSourceSchema(**data_source_dict)
            data_sources.append(data_source_schema)
        
        return data_sources
    
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
        if data_source:
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
    
    async def update_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization, data_source: DataSourceUpdate, current_user: User):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source_db = result.scalar_one_or_none()
        
        if not data_source_db:
            raise HTTPException(status_code=404, detail="Data source not found")

        # Extract the update data
        update_data = data_source.dict(exclude_unset=True)
        
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
            await db.refresh(data_source_db)
            return data_source_db
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

    async def get_data_source_fresh_schema(self, db: AsyncSession, data_source_id: str, organization: Organization):
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
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
        try:
            tables = await self.get_data_source_fresh_schema(db=db, data_source_id=data_source.id, organization=organization)
            
            if not tables:
                return
            
            # Get existing tables and their active status before deletion
            existing_tables = await db.execute(
                select(DataSourceTable).where(DataSourceTable.datasource_id == data_source.id)
            )
            active_status = {table.name: table.is_active for table in existing_tables.scalars().all()}
            
            # Delete existing tables for this datasource
            await db.execute(
                delete(DataSourceTable).where(DataSourceTable.datasource_id == data_source.id)
            )
            
            table_objects = []
            for table in tables:
                # Convert TableColumn objects to dictionaries
                if isinstance(table, dict):
                    columns = table.get("columns", {})
                    columns_dict = [{"name": col.name, "dtype": col.dtype} if hasattr(col, 'name') else col 
                                  for col in columns]
                    pks = table.get("pks", []) or []  # Convert None to []
                    pks_dict = [{"name": pk.name, "dtype": pk.dtype} if hasattr(pk, 'name') else pk 
                               for pk in pks]
                    fks = table.get("fks", []) or []  # Convert None to []
                    table_name = table.get("name")
                else:
                    columns = getattr(table, "columns", {})
                    columns_dict = [{"name": col.name, "dtype": col.dtype} if hasattr(col, 'name') else col 
                                  for col in columns]
                    pks = getattr(table, "pks", []) or []  # Convert None to []
                    pks_dict = [{"name": pk.name, "dtype": pk.dtype} if hasattr(pk, 'name') else pk 
                               for pk in pks]
                    fks = getattr(table, "fks", []) or []  # Convert None to []
                    table_name = getattr(table, "name", None)
                
                if table_name:  # Only add if name is present
                    table_object = DataSourceTable(
                        name=table_name,
                        columns=columns_dict,
                        pks=pks_dict,
                        fks=fks,
                        datasource_id=data_source.id,
                        is_active=active_status.get(table_name, should_set_active) 
                    )
                    table_objects.append(table_object)
            
            if table_objects:
                db.add_all(table_objects)
                await db.commit()

        except Exception as e:
            print(f"Error saving tables: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save database tables: {str(e)}"
            )
        
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

