from sqlalchemy import Table, Column, ForeignKey, String
from .base import BaseSchema

# Many-to-many: a catalog Prompt can target multiple agents (data sources),
# mirroring report_data_source_association.
prompt_data_source_association = Table(
    'prompt_data_source_association',
    BaseSchema.metadata,
    Column('prompt_id', String(36), ForeignKey('prompts.id')),
    Column('data_source_id', String(36), ForeignKey('data_sources.id'))
)
