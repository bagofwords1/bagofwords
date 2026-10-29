from sqlalchemy import Column, String, ForeignKey, JSON, Boolean, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema
from app.ai.prompt_formatters import Routine, RoutineParam


class ConnectionRoutine(BaseSchema):
    """A stored procedure / function discovered from a database connection.

    Reference material, deliberately NOT a `ConnectionTable`: a routine is not a
    queryable relation, so it must never pick up the table machinery (FKs,
    centrality, `DataSourceTable` activation, DDL rendering the agent would try
    to SELECT). It holds only the catalog — name, type, signature — never the
    body. The source text is large and fetched live on demand through the
    client's `get_routine_definition`, so a huge or sensitive procedure body is
    never persisted here.
    """
    __tablename__ = 'connection_routines'
    __table_args__ = (
        # One row per routine per connection. Enforced in the DB (unlike
        # ConnectionTable, this is a brand-new table with no legacy duplicates
        # to migrate around).
        UniqueConstraint('connection_id', 'name', name='uq_connection_routines_conn_name'),
    )

    connection_id = Column(String(36), ForeignKey('connections.id'), nullable=False, index=True)

    # Qualified name: "schema.name" (matches how the client renders it).
    name = Column(String, nullable=False)
    schema_name = Column(String, nullable=True)
    # 'procedure' | 'function'
    routine_type = Column(String, nullable=False, default='procedure', server_default='procedure')
    language = Column(String, nullable=True)
    params = Column(JSON, nullable=True)          # [{name, dtype, mode}]
    # Pre-rendered argument string (engines that print their own signature).
    arguments = Column(Text, nullable=True)
    returns = Column(String, nullable=True)       # return type (functions)
    description = Column(Text, nullable=True)
    # False when the routine exists but its source is not readable by the
    # indexing account (e.g. missing VIEW DEFINITION). Surfaced in the catalog
    # so the body-fetch tool can explain why it can't show the source.
    definition_available = Column(Boolean, nullable=False, default=True, server_default='1')

    metadata_json = Column(JSON, nullable=True)

    connection = relationship("Connection", back_populates="connection_routines")

    def to_prompt_routine(self) -> Routine:
        """Convert to the prompt-formatter Routine (catalog only, no body)."""
        params = [
            RoutineParam(
                name=p.get('name'),
                dtype=p.get('dtype'),
                mode=p.get('mode'),
            )
            for p in (self.params or [])
            if isinstance(p, dict)
        ]
        return Routine(
            name=self.name,
            schema_name=self.schema_name,
            routine_type=self.routine_type or 'procedure',
            language=self.language,
            params=params or None,
            arguments=self.arguments,
            returns=self.returns,
            description=self.description,
            definition_available=bool(self.definition_available),
            metadata_json=self.metadata_json,
        )
