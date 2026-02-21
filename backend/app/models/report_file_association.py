from sqlalchemy import Table, Column, Integer, ForeignKey, String
from .base import BaseSchema

report_file_association = Table(
    'report_file_association',
    BaseSchema.metadata,
    Column('report_id', String(36), ForeignKey('reports.id')),
    Column('file_id', String(36), ForeignKey('files.id')),
    Column('completion_id', String(36), ForeignKey('completions.id'), nullable=True)
)