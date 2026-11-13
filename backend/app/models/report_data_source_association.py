from sqlalchemy import Table, Column, Integer, ForeignKey, String, Index
from .base import BaseSchema

report_data_source_association = Table(
    'report_data_source_association',
    BaseSchema.metadata,
    Column('report_id', String(36), ForeignKey('reports.id')),
    Column('data_source_id', String(36), ForeignKey('data_sources.id')),
    # The dashboards list's agent filter looks a conversation's agents up (dashlist02).
    Index('ix_report_data_source_association_report_ds', 'report_id', 'data_source_id'),
)
