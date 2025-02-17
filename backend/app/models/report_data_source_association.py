from sqlalchemy import Table, Column, Integer, ForeignKey, String
from .base import BaseSchema

report_data_source_association = Table(
    'report_data_source_association',
    BaseSchema.metadata,
    Column('report_id', String(36), ForeignKey('reports.id')),
    Column('data_source_id', String(36), ForeignKey('data_sources.id'))
)