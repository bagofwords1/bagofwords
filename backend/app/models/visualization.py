from sqlalchemy import Column, String, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class Visualization(BaseSchema):
    __tablename__ = 'visualizations'

    # Basic identity and status
    title = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default='draft')

    # Ownership
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    report = relationship("Report", back_populates="visualizations", lazy="selectin")

    query_id = Column(String(36), ForeignKey('queries.id'), nullable=False, index=True)
    query = relationship("Query", back_populates="visualizations", lazy="selectin")

    # Visualization configuration
    view = Column(JSON, nullable=True, default=dict)


