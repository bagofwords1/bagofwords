from sqlalchemy import Column, Integer, String, ForeignKey, UUID, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

class Plan(BaseSchema):
    __tablename__ = 'plans'
    # table for documenting completion plans, mostly internal and debug use

    content = Column(JSON, nullable=False)
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=True)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
