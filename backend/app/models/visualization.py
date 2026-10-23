from sqlalchemy import CheckConstraint, Column, String, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class Visualization(BaseSchema):
    __tablename__ = 'visualizations'
    __table_args__ = (
        CheckConstraint('report_id IS NULL OR artifact_release_id IS NULL',
                        name='ck_visualizations_single_owner'),
    )

    # Basic identity and status
    title = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default='draft')

    # Ownership: the conversation, or (dashfc01; not written yet) the release
    # this chart is a frozen copy for -- never both.
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    report = relationship("Report", back_populates="visualizations", lazy="selectin")
    artifact_release_id = Column(String(36), ForeignKey('artifact_releases.id',
                                                        name='fk_visualizations_artifact_release_id'),
                                 nullable=True, index=True)

    query_id = Column(String(36), ForeignKey('queries.id'), nullable=False, index=True)
    query = relationship("Query", back_populates="visualizations", lazy="selectin")

    # Visualization configuration
    view = Column(JSON, nullable=True, default=dict)

    # The chart's key across copies (collab01): Publish, Edit and a joining
    # conversation copy a chart under a new id, and the copy keeps its
    # source's key, so "who changed this chart" can follow it. NULL reads as
    # the chart's own id (element_key()).
    element_key = Column(String(36), nullable=True)


