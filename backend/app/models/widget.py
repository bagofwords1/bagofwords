# Path: backend/app/models/widget.py
from sqlalchemy import Column, Integer, String, ForeignKey, UUID, Boolean
from sqlalchemy.orm import relationship
from .base import BaseSchema

class Widget(BaseSchema):
    __tablename__ = 'widgets'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    x = Column(Integer, nullable=False, default=0)
    y = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=False, default=5)
    height = Column(Integer, nullable=False, default=9)

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    report = relationship("Report", back_populates="widgets")
    
    # Use string reference to avoid circular import issues
    steps = relationship("Step", back_populates="widget", lazy="selectin")
    completions = relationship("Completion", back_populates="widget")
