# Path: backend/app/models/text_widget.py
from sqlalchemy import Column, Integer, String, ForeignKey, UUID, Boolean, JSON
from sqlalchemy.orm import relationship
from .base import BaseSchema

class TextWidget(BaseSchema):
    __tablename__ = 'text_widgets'

    status = Column(String, nullable=False, default='published')
    x = Column(Integer, nullable=False, default=0)
    y = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=False, default=5)
    height = Column(Integer, nullable=False, default=9)
    content = Column(String, nullable=False, default="")
    view = Column(JSON, nullable=True, default=dict)

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    report = relationship("Report", back_populates="text_widgets")
