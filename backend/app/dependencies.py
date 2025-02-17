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
    organization_id: Optional[str] = request.headers.get("X-Organization-Id")

    if not organization_id:
        raise HTTPException(status_code=400, detail="Organization ID header missing")

    # Convert string to UUID before querying
    try:
        organization_id = str(organization_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Organization ID format")

    organization = await db.execute(select(Organization).filter(Organization.id == organization_id))
    organization = organization.scalar_one_or_none()

    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    return organization
