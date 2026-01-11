"""
Connection Service - Handles connection-level operations.
Extracted from DataSourceService for the domain-connection architecture.
"""
import importlib
import logging
import json
from datetime import datetime
from typing import List, Optional
from uuid import UUID
import uuid as uuid_module

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_connection_overlay import UserConnectionTable, UserConnectionColumn
from app.schemas.data_source_registry import resolve_client_class, list_available_data_sources

logger = logging.getLogger(__name__)


class ConnectionService:
    """Service for managing database connections."""

    def __init__(self):
        pass

    async def create_connection(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        name: str,
        type: str,
        config: dict,
        credentials: dict = None,
        auth_policy: str = "system_only",
        allowed_user_auth_modes: list = None,
    ) -> Connection:
        """Create a new connection with validation."""
        
        # Validate connection before saving (for system_only auth)
        if auth_policy == "system_only":
            validation_result = self.test_connection_params(
                data_source_type=type,
                config=config,
                credentials=credentials,
            )
            if not validation_result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=validation_result.get("message", "Connection validation failed")
                )

        # Auto-generate connection name as type-NUMBER if not provided or generic
        connection_name = name
        if not name or name.strip() == "" or name.lower().startswith("my "):
            from sqlalchemy import func as sql_func
            count_result = await db.execute(
                select(sql_func.count(Connection.id)).filter(
                    Connection.organization_id == organization.id,
                    Connection.type == type
                )
            )
            existing_count = count_result.scalar() or 0
            connection_name = f"{type}-{existing_count + 1}"

        connection = Connection(
            name=connection_name,
            type=type,
            config=json.dumps(config) if isinstance(config, dict) else config,
            auth_policy=auth_policy,
            allowed_user_auth_modes=allowed_user_auth_modes,
            organization_id=organization.id,
            is_active=True,
        )

        if credentials:
            connection.encrypt_credentials(credentials)

        db.add(connection)
        
        try:
            await db.commit()
            await db.refresh(connection)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"A connection named '{name}' already exists in this organization."
            )

        # Discover and save tables for system_only connections
        if auth_policy == "system_only":
            await self.refresh_schema(db=db, connection=connection)

        # Re-fetch with eager loading to avoid lazy load issues in async context
        return await self.get_connection(db, str(connection.id), organization)

    async def get_connection(
        self,
        db: AsyncSession,
        connection_id: str,
        organization: Organization,
    ) -> Connection:
        """Get a connection by ID."""
        result = await db.execute(
            select(Connection)
            .options(
                selectinload(Connection.connection_tables),
                selectinload(Connection.data_sources),
            )
            .filter(
                Connection.id == connection_id,
                Connection.organization_id == organization.id
            )
        )
        connection = result.scalar_one_or_none()
        
        if not connection:
            raise HTTPException(status_code=404, detail="Connection not found")
        
        return connection

    async def get_connections(
        self,
        db: AsyncSession,
        organization: Organization,
    ) -> List[Connection]:
        """Get all connections for an organization."""
        from sqlalchemy.orm import selectinload
        # NOTE: Do NOT use selectinload for DataSource.tables here
        # For data sources with 25K+ tables, this causes severe performance issues
        # Table counts are fetched separately via COUNT query in the route
        result = await db.execute(
            select(Connection)
            .filter(Connection.organization_id == organization.id)
            .options(
                selectinload(Connection.connection_tables),
                selectinload(Connection.data_sources),
            )
            .order_by(Connection.name)
        )
        return result.scalars().all()

    async def update_connection(
        self,
        db: AsyncSession,
        connection_id: str,
        organization: Organization,
        current_user: User,
        **updates,
    ) -> Connection:
        """Update a connection."""
        connection = await self.get_connection(db, connection_id, organization)

        # Track if connection-relevant fields changed
        connection_changed = False

        if "config" in updates:
            new_config = updates.pop("config")
            connection.config = json.dumps(new_config) if isinstance(new_config, dict) else new_config
            connection_changed = True

        if "credentials" in updates:
            new_credentials = updates.pop("credentials")
            if new_credentials and not any(v is None for v in new_credentials.values()):
                connection.encrypt_credentials(new_credentials)
                connection_changed = True

        for field, value in updates.items():
            if value is not None and hasattr(connection, field):
                setattr(connection, field, value)

        # Revalidate if connection fields changed
        if connection_changed and connection.auth_policy == "system_only":
            current_config = json.loads(connection.config) if isinstance(connection.config, str) else connection.config
            current_credentials = connection.decrypt_credentials()
            
            validation_result = self.test_connection_params(
                data_source_type=connection.type,
                config=current_config,
                credentials=current_credentials,
            )
            
            if not validation_result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Updated configuration is invalid: {validation_result.get('message')}"
                )

        try:
            await db.commit()

            # Refresh tables if connection changed
            if connection_changed and connection.auth_policy == "system_only":
                await self.refresh_schema(db=db, connection=connection)

            # Re-fetch with eager loading to avoid lazy load issues in async context
            return await self.get_connection(db, connection_id, organization)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Another connection with this name already exists."
            )

    async def delete_connection(
        self,
        db: AsyncSession,
        connection_id: str,
        organization: Organization,
        current_user: User,
    ) -> dict:
        """Delete a connection and all related data."""
        connection = await self.get_connection(db, connection_id, organization)

        # Check if connection is used by any domains
        if connection.data_sources:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot delete connection that is linked to {len(connection.data_sources)} domain(s). Remove domain links first."
            )

        # Tables and credentials will cascade delete

        await db.delete(connection)
        await db.commit()

        return {"message": "Connection deleted successfully"}

    def test_connection_params(
        self,
        data_source_type: str,
        config: dict,
        credentials: dict,
    ) -> dict:
        """Test connection with given parameters (before saving)."""
        try:
            client = self._resolve_client_by_type(
                data_source_type=data_source_type,
                config=config,
                credentials=credentials,
            )

            # Test basic connectivity
            connection_status = client.test_connection()
            if not connection_status.get("success"):
                return connection_status

            # Validate schema access
            schema_status = self._validate_schema_access(client)
            
            if not schema_status.get("success"):
                return {
                    "success": False,
                    "message": schema_status.get("message", "Schema validation failed"),
                    "connectivity": True,
                    "schema_access": False,
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

    async def test_connection(
        self,
        db: AsyncSession,
        connection_id: str,
        organization: Organization,
        current_user: User = None,
    ) -> dict:
        """Test an existing connection."""
        connection = await self.get_connection(db, connection_id, organization)

        try:
            client = await self.construct_client(db, connection, current_user)
            connection_status = client.test_connection()

            success = bool(connection_status.get("success")) if isinstance(connection_status, dict) else bool(connection_status)

            # Update is_active for system_only connections
            if connection.auth_policy == "system_only":
                if not success and connection.is_active:
                    connection.is_active = False
                    await db.commit()
                elif success and not connection.is_active:
                    connection.is_active = True
                    await db.commit()

            return connection_status

        except Exception as e:
            if connection.auth_policy == "system_only":
                connection.is_active = False
                await db.commit()

            return {
                "success": False,
                "message": str(e)
            }

    async def refresh_schema(
        self,
        db: AsyncSession,
        connection: Connection,
        current_user: User = None,
    ) -> List[ConnectionTable]:
        """Refresh schema and update ConnectionTable records."""
        try:
            logger.info(f"refresh_schema: Starting for connection {connection.id} (type={connection.type}, auth_policy={connection.auth_policy})")
            client = await self.construct_client(db, connection, current_user)
            logger.info(f"refresh_schema: Client constructed successfully, calling get_schemas()...")
            fresh_tables = client.get_schemas()

            logger.info(f"refresh_schema: Got {len(fresh_tables) if fresh_tables else 0} tables from database")
            if fresh_tables and len(fresh_tables) > 0:
                logger.info(f"refresh_schema: First table name: {getattr(fresh_tables[0], 'name', 'N/A')}")

            if not fresh_tables:
                logger.warning(f"refresh_schema: No tables returned from get_schemas()")
                return []

            # Normalize incoming tables
            def normalize_columns(cols):
                return [
                    {"name": (c.name if hasattr(c, "name") else c.get("name")), 
                     "dtype": (c.dtype if hasattr(c, "dtype") else c.get("dtype"))}
                    for c in cols or []
                ]

            incoming = {}
            for t in fresh_tables:
                if isinstance(t, dict):
                    name = t.get("name")
                    if not name:
                        continue
                    incoming[name] = {
                        "columns": normalize_columns(t.get("columns", [])),
                        "pks": normalize_columns(t.get("pks", [])),
                        "fks": t.get("fks", []) or [],
                        "metadata_json": t.get("metadata_json"),
                    }
                else:
                    name = getattr(t, "name", None)
                    if not name:
                        continue
                    incoming[name] = {
                        "columns": normalize_columns(getattr(t, "columns", [])),
                        "pks": normalize_columns(getattr(t, "pks", [])),
                        "fks": getattr(t, "fks", []) or [],
                        "metadata_json": getattr(t, "metadata_json", None),
                    }

            # Get existing tables - ensure connection_id is string
            connection_id_str = str(connection.id)
            logger.info(f"refresh_schema: Looking for existing tables with connection_id={connection_id_str}")

            existing_q = await db.execute(
                select(ConnectionTable)
                .filter(ConnectionTable.connection_id == connection_id_str)
            )
            existing_tables = {t.name: t for t in existing_q.scalars().all()}
            logger.info(f"refresh_schema: Found {len(existing_tables)} existing ConnectionTable records")

            # Upsert tables
            created_count = 0
            updated_count = 0
            for name, payload in incoming.items():
                if name in existing_tables:
                    # Update existing
                    table = existing_tables[name]
                    table.columns = payload["columns"]
                    table.pks = payload["pks"]
                    table.fks = payload["fks"]
                    table.metadata_json = payload.get("metadata_json")
                    updated_count += 1
                else:
                    # Create new
                    table = ConnectionTable(
                        name=name,
                        connection_id=connection_id_str,
                        columns=payload["columns"],
                        pks=payload["pks"],
                        fks=payload["fks"],
                        metadata_json=payload.get("metadata_json"),
                        no_rows=0,
                    )
                    db.add(table)
                    created_count += 1

            logger.info(f"refresh_schema: Created {created_count}, updated {updated_count} ConnectionTable records")

            # Delete ConnectionTable entries for tables that no longer exist in the database
            deleted_count = 0
            for existing_name, existing_table in existing_tables.items():
                if existing_name not in incoming:
                    await db.delete(existing_table)
                    deleted_count += 1
            if deleted_count > 0:
                logger.info(f"refresh_schema: Deleted {deleted_count} ConnectionTable records for tables no longer in database")

            # Update last_synced_at
            # NOTE: our SQLAlchemy DateTime columns are stored as TIMESTAMP WITHOUT TIME ZONE,
            # so we must write naive UTC datetimes (asyncpg will error on tz-aware datetimes).
            connection.last_synced_at = datetime.utcnow()
            logger.info(f"refresh_schema: Committing {created_count} new tables to database...")
            await db.commit()
            logger.info(f"refresh_schema: Commit successful")

            # Return all tables
            result = await db.execute(
                select(ConnectionTable)
                .filter(ConnectionTable.connection_id == connection_id_str)
            )
            final_tables = result.scalars().all()
            logger.info(f"refresh_schema: Final query returned {len(final_tables)} ConnectionTable records")
            return final_tables

        except Exception as e:
            logger.error(f"Error refreshing schema for connection {connection.id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to refresh schema: {e}")

    async def construct_client(
        self,
        db: AsyncSession,
        connection: Connection,
        current_user: User = None,
    ):
        """Construct a database client for this connection."""
        logger.info(f"construct_client: Building client for connection {connection.id} (type={connection.type})")
        ClientClass = resolve_client_class(connection.type)
        logger.info(f"construct_client: Resolved ClientClass={ClientClass.__name__}")

        config = json.loads(connection.config) if isinstance(connection.config, str) else (connection.config or {})
        logger.info(f"construct_client: Config keys={list(config.keys()) if config else []}")

        creds = await self.resolve_credentials(db, connection, current_user)
        logger.info(f"construct_client: Credentials resolved, keys={list(creds.keys()) if creds else []}")

        params = {**(config or {}), **(creds or {})}

        # Strip meta keys
        meta_keys = {"auth_type", "auth_policy", "allowed_user_auth_modes"}
        params = {k: v for k, v in params.items() if v is not None and k not in meta_keys}

        # Narrow to constructor signature
        try:
            import inspect
            sig = inspect.signature(ClientClass.__init__)
            allowed = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}
        except Exception:
            allowed = params

        logger.info(f"construct_client: Final param keys={list(allowed.keys())}")
        return ClientClass(**allowed)

    async def resolve_credentials(
        self,
        db: AsyncSession,
        connection: Connection,
        current_user: User = None,
    ) -> dict:
        """Resolve credentials for a connection based on auth policy."""
        if connection.auth_policy == "system_only":
            return connection.decrypt_credentials()

        # user_required - need per-user credentials
        if not current_user:
            raise HTTPException(status_code=403, detail="User credentials required")

        result = await db.execute(
            select(UserConnectionCredentials)
            .where(
                UserConnectionCredentials.connection_id == connection.id,
                UserConnectionCredentials.user_id == current_user.id,
                UserConnectionCredentials.is_active == True,
            )
            .order_by(
                UserConnectionCredentials.is_primary.desc(),
                UserConnectionCredentials.updated_at.desc()
            )
        )
        row = result.scalars().first()
        
        if not row:
            raise HTTPException(
                status_code=403,
                detail="User credentials required for this connection"
            )
            
        return row.decrypt_credentials()

    def _resolve_client_by_type(
        self,
        data_source_type: str,
        config: dict,
        credentials: dict,
    ):
        """Dynamically import and construct the client for a given type."""
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

            # Strip meta keys
            meta_keys = {"auth_type", "auth_policy", "allowed_user_auth_modes"}
            client_params = {k: v for k, v in client_params.items() if k not in meta_keys}

            return ClientClass(**client_params)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Unable to load client for {data_source_type}: {str(e)}")

    def _validate_schema_access(self, client) -> dict:
        """Validate that we can read schema metadata."""
        try:
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

            if table_count == 0:
                return {
                    "success": False,
                    "message": "Connected but no tables found. Check schema name or permissions.",
                    "table_count": 0,
                }

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

