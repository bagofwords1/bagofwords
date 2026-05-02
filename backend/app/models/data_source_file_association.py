from sqlalchemy import Table, Column, ForeignKey, String
from .base import BaseSchema

data_source_file_association = Table(
    'data_source_file_association',
    BaseSchema.metadata,
    Column('data_source_id', String(36), ForeignKey('data_sources.id')),
    Column('file_id', String(36), ForeignKey('files.id')),
)
