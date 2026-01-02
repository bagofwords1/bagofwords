from sqlalchemy import Column, String, ForeignKey, JSON, Integer, Float, DateTime, Boolean
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey as PromptForeignKey


class ConnectionTable(BaseSchema):
    """
    Represents a table discovered from a database connection.
    Stores the actual schema information (columns, PKs, FKs, metrics).
    DataSourceTable (DomainTable) references this to enable per-domain table activation.
    """
    __tablename__ = 'connection_tables'

    name = Column(String, nullable=False)
    connection_id = Column(String(36), ForeignKey('connections.id'), nullable=False, index=True)
    
    # Schema information
    columns = Column(JSON, nullable=False)  # List of {name, dtype, ...}
    pks = Column(JSON, nullable=False)  # Primary keys
    fks = Column(JSON, nullable=False)  # Foreign keys
    no_rows = Column(Integer, nullable=False, default=0)
    
    # Topology and richness metrics (computed on schema refresh)
    centrality_score = Column(Float, nullable=True)
    richness = Column(Float, nullable=True)
    degree_in = Column(Integer, nullable=True)
    degree_out = Column(Integer, nullable=True)
    entity_like = Column(Boolean, nullable=True)
    metrics_computed_at = Column(DateTime, nullable=True)
    
    # Additional metadata
    metadata_json = Column(JSON, nullable=True)
    
    # Relationships
    connection = relationship("Connection", back_populates="connection_tables")
    
    # Domain tables that reference this connection table
    domain_tables = relationship(
        "DataSourceTable",
        back_populates="connection_table",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )
    
    # User-level table overlays
    user_overlays = relationship(
        "UserConnectionTable",
        back_populates="connection_table",
        cascade="all, delete-orphan"
    )

    def to_prompt_table(self) -> Table:
        """Convert to prompt formatter Table model."""
        columns = [
            TableColumn(name=col['name'], dtype=col.get('dtype'))
            for col in self.columns
        ]
        
        pks = [
            TableColumn(name=pk['name'], dtype=pk.get('dtype'))
            for pk in self.pks
        ]
        
        fks = [
            PromptForeignKey(
                column=TableColumn(name=fk['column']['name'], dtype=fk['column'].get('dtype')),
                references_name=fk['references_name'],
                references_column=TableColumn(
                    name=fk['references_column']['name'],
                    dtype=fk['references_column'].get('dtype')
                )
            )
            for fk in self.fks
        ]

        return Table(
            name=self.name,
            columns=columns,
            pks=pks,
            fks=fks,
            metadata_json=self.metadata_json
        )

