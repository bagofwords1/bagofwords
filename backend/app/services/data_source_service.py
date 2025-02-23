import importlib
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource, DATA_SOURCE_DETAILS

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.schemas.data_source_schema import DataSourceCreate, DataSourceBase, DataSourceSchema, DataSourceUpdate

from pydantic import BaseModel
from app.ai.agents.data_source.data_source import DataSourceAgent
from fastapi import HTTPException

import uuid
from uuid import UUID
import json

from app.schemas.datasource_table_schema import DataSourceTableCreateSchema
from app.models.datasource_table import DataSourceTable
from sqlalchemy import select, and_
from datetime import datetime, timedelta

import asyncio

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
        data_source_agent = DataSourceAgent(data_source=data_source, model=model)
        response = {}
        if item == "summary":
            response["summary"] = data_source_agent.generate_summary()
        elif item == "conversation_starters":
            response["conversation_starters"] = data_source_agent.generate_conversation_starters()
        elif item == "description":
            response["description"] = data_source_agent.generate_description()

        return response

    async def get_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization): 
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            return None
        
        # Convert to schema
        ds_schema = DataSourceSchema.from_orm(data_source)
        
        # Ensure config is properly parsed
        if isinstance(ds_schema.config, str):
            try:
                ds_schema.config = json.loads(ds_schema.config)
            except json.JSONDecodeError:
                raise HTTPException(status_code=500, detail="Invalid config format in database")
        
        return ds_schema

    async def get_available_data_sources(self, db: AsyncSession, organization: Organization):
        return [ds for ds in DATA_SOURCE_DETAILS if ds["status"] == "active"]
    
    async def get_data_sources(self, db: AsyncSession, current_user: User, organization: Organization):
        result = await db.execute(select(DataSource).filter(DataSource.organization_id == organization.id))
        ds = result.scalars().all()
        return [DataSourceSchema.from_orm(d) for d in ds]

    async def get_active_data_sources(self, db: AsyncSession, organization: Organization):
        result = await db.execute(select(DataSource).filter(DataSource.organization_id == organization.id, DataSource.is_active == True))
        ds = result.scalars().all()

        return [DataSourceSchema.from_orm(d) for d in ds]
    
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
            await db.delete(data_source)
            await db.commit()
        return {"message": "Data source deleted successfully"}
    
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

        return main_model_schema

    async def get_data_source_schema(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        """Get schema from database tables."""
        result = await db.execute(
            select(DataSourceTable)
            .filter(
                DataSourceTable.datasource_id == data_source_id,
                DataSourceTable.is_active == True
            )
        )
        tables = result.scalars().all()
        
        if not tables:
            # If no tables found, try to load fresh schemas
            return await self.get_fresh_schema(db, data_source_id, organization, current_user)
        
        return [table.to_prompt_table() for table in tables]

    async def get_fresh_schema(self, db: AsyncSession, data_source_id: str, organization: Organization, current_user: User):
        """Get schema directly from data source."""
        result = await db.execute(select(DataSource).filter(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
        data_source = result.scalar_one_or_none()
        
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        
        client = data_source.get_client()
        try:
            schema = client.get_schemas()
            # Save the fresh schema to database
            await self.save_schema_tables(db, data_source_id, schema)
            return schema
        except Exception as e:
            print(f"Error getting data source schema: {e}")
            raise HTTPException(status_code=500, detail=f"Error getting data source schema: {e}")

    async def save_schema_tables(self, db: AsyncSession, data_source_id: str, schema_tables: list[Table]):
        """Save or update schema tables in database."""
        # Get existing tables
        result = await db.execute(
            select(DataSourceTable)
            .filter(DataSourceTable.datasource_id == data_source_id)
        )
        existing_tables = {table.name: table for table in result.scalars().all()}
        
        # Track processed tables
        processed_tables = set()
        
        # Update or create tables
        for table in schema_tables:
            table_data = {
                "name": table.name,
                "columns": [{"name": col.name, "dtype": col.dtype} for col in table.columns] if table.columns else [],
                "pks": [{"name": pk.name, "dtype": pk.dtype} for pk in table.pks] if table.pks else [],
                "fks": [{
                    "column": {"name": fk.column.name, "dtype": fk.column.dtype},
                    "references_name": fk.references_name,
                    "references_column": {"name": fk.references_column.name, "dtype": fk.references_column.dtype}
                } for fk in table.fks] if table.fks else [],
                "datasource_id": data_source_id,
                "is_active": True
            }
            
            if table.name in existing_tables:
                # Update existing table
                existing_table = existing_tables[table.name]
                for key, value in table_data.items():
                    setattr(existing_table, key, value)
            else:
                # Create new table
                new_table = DataSourceTable(**table_data)
                db.add(new_table)
            
            processed_tables.add(table.name)
        
        # Deactivate tables that no longer exist
        for table_name, table in existing_tables.items():
            if table_name not in processed_tables:
                table.is_active = False
        
        await db.commit()

    async def refresh_all_schemas(self, db: AsyncSession):
        """Refresh schemas for all active data sources."""
        result = await db.execute(
            select(DataSource)
            .filter(DataSource.is_active == True)
        )
        data_sources = result.scalars().all()
        
        for data_source in data_sources:
            try:
                client = data_source.get_client()
                schema = client.get_schemas()
                await self.save_schema_tables(db, data_source.id, schema)
            except Exception as e:
                print(f"Error refreshing schema for data source {data_source.id}: {e}")
                continue

    async def schedule_schema_refresh(self):
        """Schedule daily schema refresh."""
        while True:
            try:
                # Create new session for the background task
                async with AsyncSession() as db:
                    await self.refresh_all_schemas(db)
            except Exception as e:
                print(f"Error in scheduled schema refresh: {e}")
            
            # Wait for 24 hours
            await asyncio.sleep(24 * 60 * 60)