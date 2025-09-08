from sqlalchemy import Column, String, Integer, Boolean, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class DashboardLayoutVersion(BaseSchema):
    __tablename__ = 'dashboard_layout_versions'

    name = Column(String, nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=False)

    theme_name = Column(String, nullable=True, default=None)
    theme_overrides = Column(JSON, nullable=True, default=dict)

    # Array of blocks describing layout
    # Example block: {"type": "widget", "widget_id": "...", "x": 0, "y": 0, "width": 6, "height": 8}
    # Or: {"type": "text_widget", "text_widget_id": "...", ...}
    blocks = Column(JSON, nullable=False, default=list)

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    report = relationship("Report", back_populates="dashboard_layout_versions", lazy="selectin")


