from typing import AsyncGenerator
from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, async_scoped_session
from fastapi_users.db import SQLAlchemyUserDatabase, SQLAlchemyBaseOAuthAccountTableUUID
from app.settings.database import create_session_factory, create_async_session_factory, create_async_database_engine
from app.models.user import User
from app.models.organization import Organization
from fastapi import HTTPException
from fastapi import Request
from fastapi import BackgroundTasks
from typing import Optional
from sqlalchemy import select
from app.models.oauth_account import OAuthAccount

from app.settings import config
from app.errors import AppError, ErrorCode

# Create a session factory at the start to reuse
SessionLocal = create_session_factory()

# Create an async session factory at the start to reuse
# async_session_maker = create_async_session_factory()
# Create an async engine
engine = create_async_database_engine()

# Create an async session maker
async_session_maker = create_async_session_factory()

async def get_db():
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()

async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session

async def get_async_db() -> AsyncSession:
    async with async_session_maker() as session:
        yield session

async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, User, OAuthAccount)

async def get_current_organization(request: Request, db: AsyncSession = Depends(get_async_db)) -> Organization:
    """Get organization from X-Organization-Id header or from API key."""
    organization_id: Optional[str] = request.headers.get("X-Organization-Id")

    if organization_id:
        # Header provided - use it
        organization = await db.execute(select(Organization).filter(Organization.id == organization_id))
        organization = organization.scalar_one_or_none()
        if not organization:
            raise AppError.not_found(ErrorCode.ORG_NOT_FOUND, "Organization not found")
        return organization

    # No header - try to get from API key
    api_key = request.headers.get("X-API-Key") or ""
    auth_header = request.headers.get("Authorization", "")

    if api_key.startswith("bow_") or auth_header.startswith("Bearer bow_"):
        from app.services.api_key_service import ApiKeyService
        api_key_service = ApiKeyService()

        key = api_key if api_key.startswith("bow_") else auth_header[7:]
        org = await api_key_service.get_organization_by_api_key(db, key)
        if org:
            return org
        # API key was provided but is invalid/expired
        raise AppError.unauthorized(ErrorCode.API_KEY_INVALID, "Invalid or expired API key")

    raise AppError.bad_request(ErrorCode.ORG_HEADER_REQUIRED, "Organization ID header missing")


def _locale_from_org(organization: Optional[Organization]) -> Optional[str]:
    """Extract org's configured locale, if any. Returns None when unset or invalid."""
    if organization is None or organization.settings is None:
        return None
    cfg_dict = getattr(organization.settings, "config", None) or {}
    if not isinstance(cfg_dict, dict):
        return None
    candidate = cfg_dict.get("locale")
    if candidate in config.settings.bow_config.i18n.enabled_locales:
        return candidate
    return None


async def get_current_locale(request: Request) -> str:
    """Resolve the effective locale for the current request.

    Priority: X-Locale header override (must be in enabled_locales) →
    system default. Unauthed-safe: no DB access. Authed callers that need
    org-aware resolution should use `get_org_locale` instead.
    """
    enabled = config.settings.bow_config.i18n.enabled_locales
    override = request.headers.get("X-Locale")
    if override and override in enabled:
        return override
    return config.settings.bow_config.i18n.default_locale


async def get_org_locale(
    request: Request,
    organization: Organization = Depends(get_current_organization),
) -> str:
    """Effective locale for authed requests: header override → org → default."""
    enabled = config.settings.bow_config.i18n.enabled_locales
    override = request.headers.get("X-Locale")
    if override and override in enabled:
        return override
    org_locale = _locale_from_org(organization)
    if org_locale:
        return org_locale
    return config.settings.bow_config.i18n.default_locale


async def require_mcp_enabled(
    organization: Organization = Depends(get_current_organization)
) -> Organization:
    """Dependency to ensure MCP is enabled for the organization."""
    if not organization.settings:
        raise AppError.forbidden(ErrorCode.MCP_DISABLED, "MCP integration is not enabled for this organization")

    mcp_config = organization.settings.get_config("mcp_enabled")
    if not mcp_config or not getattr(mcp_config, "value", False):
        raise AppError.forbidden(ErrorCode.MCP_DISABLED, "MCP integration is not enabled for this organization")

    return organization
