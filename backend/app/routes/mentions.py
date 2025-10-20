from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.core.auth import current_user as auth_current_user
from app.dependencies import get_async_db
from app.dependencies import get_current_organization

from app.models.user import User
from app.models.organization import Organization
from app.services.mention_service import MentionService
from app.schemas.mention_schema import AvailableMentionsResponse


router = APIRouter(prefix="/mentions", tags=["mentions"])


@router.get("/available", response_model=AvailableMentionsResponse)
async def get_available_mentions(
    data_source_ids: Optional[str] = Query(
        None, 
        description="Comma-separated data source IDs to filter by (e.g., 'ds-1,ds-2')"
    ),
    categories: Optional[str] = Query(
        None,
        description="Comma-separated categories to return: data_sources, tables, files, entities (default: all)"
    ),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(auth_current_user),
    organization: Organization = Depends(get_current_organization)
):
    """
    Get mentionable items for the prompt box.
    
    This endpoint returns data sources, tables, files, and entities that the user
    can mention in prompts using the @ syntax.
    
    Access control:
    - Data sources: User must have access (public OR membership)
    - Tables: Filtered by accessible data sources, respects auth_policy
    - Files: Organization-level access
    - Entities: Only published entities with accessible data sources
    
    Query Parameters:
    - data_source_ids: Optional filter to scope tables/entities to specific data sources
    - categories: Optional filter to only return specific categories
    
    Returns:
    - data_sources: List of accessible data sources
    - tables: List of tables from accessible data sources
    - files: List of organization files
    - entities: List of published entities
    """
    
    # Parse comma-separated values
    ds_ids = None
    if data_source_ids:
        ds_ids = [id.strip() for id in data_source_ids.split(',') if id.strip()]
    
    cats = None
    if categories:
        cats = [cat.strip() for cat in categories.split(',') if cat.strip()]
    
    # Get mentions from unified service
    mention_service = MentionService()
    result = await mention_service.get_available_mentions(
        db=db,
        organization=organization,
        current_user=current_user,
        data_source_ids=ds_ids,
        categories=cats
    )
    
    return result

