from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.user_data_source_credentials import UserDataSourceCredentials
from app.schemas.user_data_source_credentials_schema import (
    UserDataSourceCredentialsCreate,
    UserDataSourceCredentialsUpdate,
    UserDataSourceCredentialsSchema,
)
from app.schemas.data_source_registry import get_entry
from fastapi import HTTPException
from app.schemas.data_source_schema import DataSourceUserStatus
from app.schemas.data_source_registry import resolve_client_class
import json
import inspect


class UserDataSourceCredentialsService:
    def _get_connection_info(self, data_source: DataSource) -> tuple:
        """
        Get connection info (type, config, auth_policy, allowed_user_auth_modes) from the first connection.
        Returns (type, config, auth_policy, allowed_user_auth_modes, connection) tuple.
        """
        conn = data_source.connections[0] if data_source.connections else None
        if not conn:
            return (None, {}, "system_only", None, None)
        
        config = conn.config
        if isinstance(config, str):
            config = json.loads(config)
        
        return (
            conn.type,
            config or {},
            conn.auth_policy or "system_only",
            conn.allowed_user_auth_modes,
            conn
        )

    async def get_primary_active_row(self, db: AsyncSession, data_source: DataSource, user: User) -> Optional[UserDataSourceCredentials]:
        stmt = (
            select(UserDataSourceCredentials)
            .where(
                UserDataSourceCredentials.data_source_id == data_source.id,
                UserDataSourceCredentials.user_id == user.id,
                UserDataSourceCredentials.is_active == True,
            )
            .order_by(UserDataSourceCredentials.is_primary.desc(), UserDataSourceCredentials.updated_at.desc())
        )
        return (await db.execute(stmt)).scalars().first()

    async def get_my_credentials(self, db: AsyncSession, data_source: DataSource, user: User) -> Optional[UserDataSourceCredentialsSchema]:
        row = await self.get_primary_active_row(db, data_source, user)
        return UserDataSourceCredentialsSchema.from_orm(row) if row else None

    async def build_user_status(self, db: AsyncSession, data_source: DataSource, user: User, live_test: bool = False) -> DataSourceUserStatus:
        import logging
        logger = logging.getLogger(__name__)
        
        # Get connection info from the first connection
        ds_type, config, auth_policy, allowed_user_auth_modes, connection = self._get_connection_info(data_source)
        
        # For system-only data sources, report system connection status
        if auth_policy != "user_required":
            conn_status = "unknown"
            if live_test:
                try:
                    from app.services.data_source_service import DataSourceService
                    ds_service = DataSourceService()
                    client = await ds_service.construct_client(db=db, data_source=data_source, current_user=user)
                    ok = client.test_connection()
                    success = bool(ok.get("success")) if isinstance(ok, dict) else bool(ok)
                    conn_status = "success" if success else "not_connected"
                    logger.info(f"Connection test for {data_source.name}: {conn_status} (result={ok})")
                except Exception as e:
                    logger.error(f"Connection test failed for {data_source.name}: {e}")
                    conn_status = "not_connected"
            return DataSourceUserStatus(has_user_credentials=False, connection=conn_status, effective_auth="system")

        row = await self.get_primary_active_row(db, data_source, user)
        if not row:
            # Owner fallback possible; if no creds but owner can use system, reflect that
            is_owner = str(getattr(data_source, "owner_user_id", "")) == str(getattr(user, "id", ""))
            has_system_creds = connection and connection.credentials if connection else False
            if is_owner and has_system_creds:
                conn = "unknown"
                if live_test:
                    try:
                        # Attempt live test using system credentials
                        from app.services.data_source_service import DataSourceService
                        ds_service = DataSourceService()
                        client = await ds_service.construct_client(db=db, data_source=data_source, current_user=user)
                        ok = client.test_connection()
                        success = bool(ok.get("success")) if isinstance(ok, dict) else bool(ok)
                        conn = "success" if success else "not_connected"
                    except Exception:
                        conn = "not_connected"
                return DataSourceUserStatus(has_user_credentials=False, connection=conn, effective_auth="system", uses_fallback=True)
            return DataSourceUserStatus(has_user_credentials=False, connection="offline", effective_auth="none")

        conn = "unknown"
        if live_test:
            try:
                # Local import to avoid circular
                from app.services.data_source_service import DataSourceService
                ds_service = DataSourceService()
                client = await ds_service.construct_client(db=db, data_source=data_source, current_user=user)
                ok = client.test_connection()
                success = bool(ok.get("success")) if isinstance(ok, dict) else bool(ok)
                conn = "success" if success else "not_connected"
            except Exception:
                conn = "not_connected"

        return DataSourceUserStatus(
            has_user_credentials=True,
            auth_mode=row.auth_mode,
            is_primary=row.is_primary,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            connection=conn,
            effective_auth="user",
            uses_fallback=False,
            credentials_id=str(getattr(row, "id", "")) if getattr(row, "id", None) else None,
        )

    async def test_my_credentials(self, db: AsyncSession, data_source: DataSource, user: User, payload: UserDataSourceCredentialsCreate) -> dict:
        # Get connection info
        ds_type, config, auth_policy, allowed_user_auth_modes, connection = self._get_connection_info(data_source)
        
        if not ds_type:
            raise HTTPException(status_code=400, detail="Data source has no connection")
        
        # Validate against registry
        entry = get_entry(ds_type)
        variant = (entry.credentials_auth.by_auth or {}).get(payload.auth_mode)
        if not variant or ("user" not in (variant.scopes or [])):
            raise HTTPException(status_code=400, detail="Authentication mode is not allowed for user credentials")
        schema_cls = variant.schema
        try:
            schema_cls(**(payload.credentials or {}))
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid credentials: {e}")

        # Build client with provided creds without persisting
        ClientClass = resolve_client_class(ds_type)
        params = {**(config or {}), **(payload.credentials or {})}
        # Strip meta keys
        meta_keys = {"auth_type", "auth_policy", "allowed_user_auth_modes"}
        params = {k: v for k, v in params.items() if v is not None and k not in meta_keys}
        # Filter by signature
        try:
            sig = inspect.signature(ClientClass.__init__)
            params = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}
        except Exception:
            pass
        client = ClientClass(**params)
        try:
            res = client.test_connection()
            success = bool(res.get("success")) if isinstance(res, dict) else bool(res)
            return {"success": success, "message": (res.get("message") if isinstance(res, dict) else None)}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def upsert_my_credentials(self, db: AsyncSession, data_source: DataSource, user: User, payload: UserDataSourceCredentialsCreate) -> UserDataSourceCredentialsSchema:
        # Get connection info
        ds_type, config, auth_policy, allowed_user_auth_modes, connection = self._get_connection_info(data_source)
        
        if not ds_type:
            raise HTTPException(status_code=400, detail="Data source has no connection")
        
        # Policy: ensure auth_mode allowed for user scope
        entry = get_entry(ds_type)
        variant = (entry.credentials_auth.by_auth or {}).get(payload.auth_mode)
        if not variant or ("user" not in (variant.scopes or [])):
            raise HTTPException(status_code=400, detail="Authentication mode is not allowed for user credentials")

        # If DS restricts allowed_user_auth_modes, enforce
        allowed = allowed_user_auth_modes or []
        if allowed and payload.auth_mode not in allowed:
            raise HTTPException(status_code=400, detail="Authentication mode not permitted by data source policy")

        # Validate credentials against registry schema
        schema_cls = variant.schema
        try:
            schema_cls(**(payload.credentials or {}))
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid credentials: {e}")

        # Find existing (active) row
        stmt = (
            select(UserDataSourceCredentials)
            .where(
                UserDataSourceCredentials.data_source_id == data_source.id,
                UserDataSourceCredentials.user_id == user.id,
                UserDataSourceCredentials.is_active == True,
            )
            .order_by(UserDataSourceCredentials.is_primary.desc(), UserDataSourceCredentials.updated_at.desc())
        )
        row = (await db.execute(stmt)).scalars().first()

        if row is None:
            row = UserDataSourceCredentials(
                data_source_id=str(data_source.id),
                user_id=str(user.id),
                organization_id=str(data_source.organization_id),
                auth_mode=payload.auth_mode,
                is_active=True,
                is_primary=bool(payload.is_primary if payload.is_primary is not None else True),
                expires_at=payload.expires_at,
                metadata_json=payload.metadata_json,
            )
        else:
            row.auth_mode = payload.auth_mode
            row.is_primary = bool(payload.is_primary if payload.is_primary is not None else row.is_primary)
            row.expires_at = payload.expires_at
            row.metadata_json = payload.metadata_json

        # Encrypt secret payload
        row.encrypt_credentials(payload.credentials or {})
        db.add(row)
        await db.commit()
        await db.refresh(row)

        # Enforce single primary per user+DS
        if row.is_primary:
            await db.execute(
                update(UserDataSourceCredentials)
                .where(
                    UserDataSourceCredentials.data_source_id == data_source.id,
                    UserDataSourceCredentials.user_id == user.id,
                    UserDataSourceCredentials.id != row.id,
                )
                .values(is_primary=False)
            )
            await db.commit()

        # Refresh per-user schema overlay (best-effort)
        try:
            from app.services.data_source_service import DataSourceService
            ds_service = DataSourceService()
            await ds_service.get_user_data_source_schema(db=db, data_source=data_source, user=user)
        except Exception:
            pass

        return UserDataSourceCredentialsSchema.from_orm(row)

    async def patch_my_credentials(self, db: AsyncSession, data_source: DataSource, user: User, payload: UserDataSourceCredentialsUpdate) -> UserDataSourceCredentialsSchema:
        # Get connection info
        ds_type, config, auth_policy, allowed_user_auth_modes, connection = self._get_connection_info(data_source)
        
        if not ds_type:
            raise HTTPException(status_code=400, detail="Data source has no connection")
        
        stmt = (
            select(UserDataSourceCredentials)
            .where(
                UserDataSourceCredentials.data_source_id == data_source.id,
                UserDataSourceCredentials.user_id == user.id,
                UserDataSourceCredentials.is_active == True,
            )
            .order_by(UserDataSourceCredentials.is_primary.desc(), UserDataSourceCredentials.updated_at.desc())
        )
        row = (await db.execute(stmt)).scalars().first()
        if not row:
            raise HTTPException(status_code=404, detail="User credentials not found")

        # If auth_mode changes, require credentials
        if payload.auth_mode and not payload.credentials:
            raise HTTPException(status_code=400, detail="credentials are required when changing auth_mode")

        # Apply changes
        if payload.auth_mode:
            entry = get_entry(ds_type)
            variant = (entry.credentials_auth.by_auth or {}).get(payload.auth_mode)
            if not variant or ("user" not in (variant.scopes or [])):
                raise HTTPException(status_code=400, detail="Authentication mode is not allowed for user credentials")
            # Validate new credentials
            schema_cls = variant.schema
            try:
                schema_cls(**(payload.credentials or {}))
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid credentials: {e}")
            row.auth_mode = payload.auth_mode
            row.encrypt_credentials(payload.credentials or {})

        if payload.credentials and not payload.auth_mode:
            # Validate against current auth_mode
            entry = get_entry(ds_type)
            variant = (entry.credentials_auth.by_auth or {}).get(row.auth_mode)
            schema_cls = variant.schema if variant else None
            if schema_cls is None:
                raise HTTPException(status_code=400, detail="Cannot validate credentials for current auth_mode")
            try:
                schema_cls(**payload.credentials)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Invalid credentials: {e}")
            row.encrypt_credentials(payload.credentials)

        if payload.is_active is not None:
            row.is_active = bool(payload.is_active)
        if payload.is_primary is not None:
            row.is_primary = bool(payload.is_primary)
        if payload.expires_at is not None:
            row.expires_at = payload.expires_at
        if payload.metadata_json is not None:
            row.metadata_json = payload.metadata_json

        db.add(row)
        await db.commit()
        await db.refresh(row)

        # Enforce single primary if set
        if row.is_primary:
            await db.execute(
                update(UserDataSourceCredentials)
                .where(
                    UserDataSourceCredentials.data_source_id == data_source.id,
                    UserDataSourceCredentials.user_id == user.id,
                    UserDataSourceCredentials.id != row.id,
                )
                .values(is_primary=False)
            )
            await db.commit()

        # Refresh per-user schema overlay (best-effort)
        try:
            from app.services.data_source_service import DataSourceService
            ds_service = DataSourceService()
            await ds_service.get_user_data_source_schema(db=db, data_source=data_source, user=user)
        except Exception:
            pass

        return UserDataSourceCredentialsSchema.from_orm(row)

    async def delete_my_credentials(self, db: AsyncSession, data_source: DataSource, user: User) -> None:
        stmt = (
            select(UserDataSourceCredentials)
            .where(
                UserDataSourceCredentials.data_source_id == data_source.id,
                UserDataSourceCredentials.user_id == user.id,
            )
            .order_by(UserDataSourceCredentials.is_primary.desc(), UserDataSourceCredentials.updated_at.desc())
        )
        row = (await db.execute(stmt)).scalars().first()
        if not row:
            return
        await db.delete(row)
        await db.commit()


