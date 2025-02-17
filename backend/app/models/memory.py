from sqlalchemy import Column, Integer, String, ForeignKey, UUID, Boolean
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

class Memory(BaseSchema):
    __tablename__ = 'memories'

    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)
    step_id = Column(String(36), ForeignKey('steps.id'), nullable=True)
    widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=True)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True)
    is_public = Column(Boolean, default=False)

    user = relationship("User", back_populates="memories", lazy="selectin")
    organization = relationship("Organization", back_populates="memories")
    step = relationship("Step", back_populates="memories")
    widget = relationship("Widget", back_populates="memories")