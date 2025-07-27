from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, update
from fastapi import HTTPException

from app.models.completion_feedback import CompletionFeedback
from app.models.completion import Completion
from app.models.user import User
from app.models.organization import Organization
from app.schemas.completion_feedback_schema import (
    CompletionFeedbackCreate, 
    CompletionFeedbackUpdate, 
    CompletionFeedbackSchema,
    CompletionFeedbackSummary
)


class CompletionFeedbackService:
    
    async def create_or_update_feedback(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        feedback_data: CompletionFeedbackCreate, 
        user: Optional[User], 
        organization: Organization
    ) -> CompletionFeedbackSchema:
        """Create or update feedback for a completion. If user already has feedback, update it."""
        
        # Verify completion exists and belongs to organization
        completion_stmt = select(Completion).where(
            Completion.id == completion_id,
            Completion.report.has(organization_id=organization.id)
        )
        completion_result = await db.execute(completion_stmt)
        completion = completion_result.scalar_one_or_none()
        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        user_id = user.id if user else None
        
        # Check if user already has feedback for this completion
        existing_feedback_stmt = select(CompletionFeedback).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.user_id == user_id,
            CompletionFeedback.organization_id == organization.id
        )
        existing_result = await db.execute(existing_feedback_stmt)
        existing_feedback = existing_result.scalar_one_or_none()
        
        if existing_feedback:
            # Update existing feedback
            existing_feedback.direction = feedback_data.direction
            existing_feedback.message = feedback_data.message
            await db.commit()
            await db.refresh(existing_feedback)
            return CompletionFeedbackSchema.from_orm(existing_feedback)
        else:
            # Create new feedback
            feedback = CompletionFeedback(
                user_id=user_id,
                completion_id=completion_id,
                organization_id=organization.id,
                direction=feedback_data.direction,
                message=feedback_data.message
            )
            
            db.add(feedback)
            await db.commit()
            await db.refresh(feedback)

            return CompletionFeedbackSchema.from_orm(feedback)
    
    async def get_feedback_summary(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        user: Optional[User], 
        organization: Organization
    ) -> CompletionFeedbackSummary:
        """Get feedback summary for a completion including user's feedback if any."""
        
        # Verify completion exists and belongs to organization
        completion_stmt = select(Completion).where(
            Completion.id == completion_id,
            Completion.report.has(organization_id=organization.id)
        )
        completion_result = await db.execute(completion_stmt)
        completion = completion_result.scalar_one_or_none()
        
        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        # Get aggregated feedback stats
        stats_stmt = select(
            func.count(CompletionFeedback.id).label('total_feedbacks'),
            func.count().filter(CompletionFeedback.direction == 1).label('total_upvotes'),
            func.count().filter(CompletionFeedback.direction == -1).label('total_downvotes'),
            func.sum(CompletionFeedback.direction).label('net_score')
        ).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.organization_id == organization.id
        )
        
        stats_result = await db.execute(stats_stmt)
        stats = stats_result.first()
        
        # Get user's feedback if user is provided
        user_feedback = None
        if user:
            user_feedback_stmt = select(CompletionFeedback).where(
                CompletionFeedback.completion_id == completion_id,
                CompletionFeedback.user_id == user.id,
                CompletionFeedback.organization_id == organization.id
            )
            user_feedback_result = await db.execute(user_feedback_stmt)
            user_feedback_obj = user_feedback_result.scalar_one_or_none()
            if user_feedback_obj:
                user_feedback = CompletionFeedbackSchema.from_orm(user_feedback_obj)
        
        return CompletionFeedbackSummary(
            completion_id=completion_id,
            total_upvotes=stats.total_upvotes or 0,
            total_downvotes=stats.total_downvotes or 0,
            net_score=stats.net_score or 0,
            total_feedbacks=stats.total_feedbacks or 0,
            user_feedback=user_feedback
        )
    
    async def delete_feedback(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        user: User, 
        organization: Organization
    ) -> bool:
        """Delete user's feedback for a completion."""
        
        feedback_stmt = select(CompletionFeedback).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.user_id == user.id,
            CompletionFeedback.organization_id == organization.id
        )
        feedback_result = await db.execute(feedback_stmt)
        feedback = feedback_result.scalar_one_or_none()
        
        if not feedback:
            raise HTTPException(status_code=404, detail="Feedback not found")
        
        await db.delete(feedback)
        await db.commit()
        return True
    
    async def get_completion_feedbacks(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        organization: Organization
    ) -> List[CompletionFeedbackSchema]:
        """Get all feedbacks for a completion."""
        
        # Verify completion exists and belongs to organization
        completion_stmt = select(Completion).where(
            Completion.id == completion_id,
            Completion.report.has(organization_id=organization.id)
        )
        completion_result = await db.execute(completion_stmt)
        completion = completion_result.scalar_one_or_none()
        
        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        feedbacks_stmt = select(CompletionFeedback).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.organization_id == organization.id
        )
        feedbacks_result = await db.execute(feedbacks_stmt)
        feedbacks = feedbacks_result.scalars().all()
        
        return [CompletionFeedbackSchema.from_orm(feedback) for feedback in feedbacks]