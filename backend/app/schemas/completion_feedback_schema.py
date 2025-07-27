from pydantic import BaseModel, validator
from typing import Optional
from datetime import datetime


class CompletionFeedbackBase(BaseModel):
    direction: int  # 1 for upvote, -1 for downvote
    message: Optional[str] = None

    @validator('direction')
    def validate_direction(cls, v):
        if v not in [1, -1]:
            raise ValueError('Direction must be 1 (upvote) or -1 (downvote)')
        return v


class CompletionFeedbackCreate(CompletionFeedbackBase):
    pass


class CompletionFeedbackUpdate(BaseModel):
    direction: Optional[int] = None
    message: Optional[str] = None

    @validator('direction')
    def validate_direction(cls, v):
        if v is not None and v not in [1, -1]:
            raise ValueError('Direction must be 1 (upvote) or -1 (downvote)')
        return v


class CompletionFeedbackSchema(CompletionFeedbackBase):
    id: str
    user_id: Optional[str] = None
    completion_id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CompletionFeedbackSummary(BaseModel):
    """Summary of all feedback for a completion"""
    completion_id: str
    total_upvotes: int
    total_downvotes: int
    net_score: int
    total_feedbacks: int
    user_feedback: Optional[CompletionFeedbackSchema] = None  # Current user's feedback if any

    class Config:
        from_attributes = True 