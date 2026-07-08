from sqlalchemy import Table, Column, ForeignKey, String
from .base import BaseSchema

# Many-to-many: a standalone trigger webhook (webhooks.report_id IS NULL) can be
# associated with multiple agents (data sources). Every session it spawns gets
# these agents attached. Mirrors prompt_data_source_association.
webhook_data_source_association = Table(
    'webhook_data_source_association',
    BaseSchema.metadata,
    Column('webhook_id', String(36), ForeignKey('webhooks.id')),
    Column('data_source_id', String(36), ForeignKey('data_sources.id'))
)
