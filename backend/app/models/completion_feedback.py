from sqlalchemy import Column, Integer, String, ForeignKey, Text, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import BaseSchema


class CompletionFeedback(BaseSchema):
    __tablename__ = 'completion_feedbacks'

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)  # Nullable for legacy 'system' feedbacks
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=False)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    direction = Column(Integer, nullable=False)  # 1 for upvote, -1 for downvote
    message = Column(Text, nullable=True)  # Optional feedback message

    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String(36), ForeignKey('users.id'), nullable=True)

    # Relationships
    user = relationship("User", back_populates="completion_feedbacks", foreign_keys=[user_id], lazy='select')
    completion = relationship("Completion", back_populates="feedbacks", lazy='select')
    organization = relationship("Organization", back_populates="completion_feedbacks", lazy='select')
    reviewed_by_user = relationship("User", back_populates="reviewed_completion_feedbacks", foreign_keys=[reviewed_by], lazy='select')

    def __repr__(self):
        return f"<CompletionFeedback(id={self.id}, user_id={self.user_id}, completion_id={self.completion_id}, direction={self.direction})>"