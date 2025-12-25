from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.services.completion_feedback_service import CompletionFeedbackService
from app.schemas.completion_feedback_schema import (
    CompletionFeedbackCreate,
    CompletionFeedbackSchema,
    CompletionFeedbackSummary
)

router = APIRouter(tags=["completion-feedback"])
feedback_service = CompletionFeedbackService()


@router.post("/completions/{completion_id}/feedback")
@requires_permission('create_completion_feedback')
async def create_or_update_feedback(
    completion_id: str,
    feedback_data: CompletionFeedbackCreate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
) -> CompletionFeedbackSchema:
    """Create or update feedback for a completion. If user already has feedback, it will be updated."""
    return await feedback_service.create_or_update_feedback(
        db, completion_id, feedback_data, current_user, organization
    )


@router.get("/completions/{completion_id}/feedback/summary")
@requires_permission('create_completion_feedback')
async def get_feedback_summary(
    completion_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
) -> CompletionFeedbackSummary:
    """Get feedback summary for a completion including current user's feedback."""
    return await feedback_service.get_feedback_summary(
        db, completion_id, current_user, organization
    )


@router.post("/completions/{completion_id}/feedback/suggest-instructions")
@requires_permission('create_completion_feedback')
async def suggest_instructions_from_feedback(
    completion_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
) -> List[dict]:
    """Generate instruction suggestions based on completion context and user feedback.
    
    This endpoint is called after negative feedback is submitted to generate
    helpful instructions that could prevent similar issues in the future.
    """
    return await feedback_service.generate_suggestions_from_feedback(
        db, completion_id, current_user, organization
    )


@router.get("/completions/{completion_id}/feedback")
@requires_permission('create_completion_feedback')
async def get_completion_feedbacks(
    completion_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
) -> List[CompletionFeedbackSchema]:
    """Get all feedbacks for a completion."""
    return await feedback_service.get_completion_feedbacks(
        db, completion_id, organization
    )


@router.delete("/completions/{completion_id}/feedback")
@requires_permission('create_completion_feedback')
async def delete_feedback(
    completion_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
) -> dict:
    """Delete current user's feedback for a completion."""
    success = await feedback_service.delete_feedback(
        db, completion_id, current_user, organization
    )
    return {"success": success}