from sqlalchemy import Table, Column, String, ForeignKey
from app.models.base import Base


# Junction table for M:N relationship between DataSource (Domain) and Connection
domain_connection = Table(
    'domain_connection',
    Base.metadata,
    Column('data_source_id', String(36), ForeignKey('data_sources.id', ondelete='CASCADE'), primary_key=True),
    Column('connection_id', String(36), ForeignKey('connections.id', ondelete='CASCADE'), primary_key=True)
)

