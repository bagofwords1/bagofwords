from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class Query(BaseSchema):
    __tablename__ = 'queries'

    # Minimal v0: treat Query as a facade for Widget ownership within a Report
    title = Column(String, nullable=False, default="")
    description = Column(String, nullable=True, default=None)

    # Owning report (optional). Allows global/public queries not tied to a report.
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    report = relationship("Report", back_populates="queries", lazy="selectin")

    

    # Transitional: 1:1 linkage with a Widget so we don't orphan Steps
    widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=False, index=True)
    widget = relationship("Widget", lazy="selectin")

    # Optional: default step for this query (version follow)
    default_step_id = Column(String(36), ForeignKey('steps.id'), nullable=True, index=True)
    # Disambiguate the Step<->Query relationships due to two FKs between these tables
    steps = relationship("Step", back_populates="query", lazy="selectin", foreign_keys="Step.query_id")
    default_step = relationship("Step", foreign_keys=[default_step_id], lazy="selectin")

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True, index=True)
    organization = relationship("Organization", back_populates="queries", lazy="selectin")

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True, index=True)
    user = relationship("User", back_populates="queries", lazy="selectin")

    # Visualizations owned by this query
    visualizations = relationship("Visualization", back_populates="query", lazy="selectin")

