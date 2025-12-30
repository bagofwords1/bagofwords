from sqlalchemy import Column, String, ForeignKey, JSON, Integer, Float, DateTime
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey as PromptForeignKey
from sqlalchemy import Boolean


class DataSourceTable(BaseSchema):
    """
    Represents a table selection within a Domain (DataSource).
    Links to ConnectionTable for actual schema and stores domain-specific activation.
    This is the "DomainTable" in the new architecture.
    """
    __tablename__ = 'datasource_tables'

    name = Column(String, nullable=False)
    datasource_id = Column(String(36), ForeignKey('data_sources.id'), nullable=False)
    
    # Reference to the actual table schema in ConnectionTable
    connection_table_id = Column(String(36), ForeignKey('connection_tables.id'), nullable=True, index=True)
    
    # Domain-specific activation (whether this table is used in this domain)
    is_active = Column(Boolean, nullable=False, default=True)
    
    # Domain-specific metadata overrides
    metadata_json = Column(JSON, nullable=True)
    
    # Legacy fields - kept for backward compatibility during migration
    # These will be removed after data migration to ConnectionTable
    columns = Column(JSON, nullable=True)  # Changed to nullable
    no_rows = Column(Integer, nullable=True, default=0)  # Changed to nullable
    pks = Column(JSON, nullable=True)  # Changed to nullable
    fks = Column(JSON, nullable=True)  # Changed to nullable
    
    # Legacy metrics - will be removed after migration
    centrality_score = Column(Float, nullable=True)
    richness = Column(Float, nullable=True)
    degree_in = Column(Integer, nullable=True)
    degree_out = Column(Integer, nullable=True)
    entity_like = Column(Boolean, nullable=True)
    metrics_computed_at = Column(DateTime, nullable=True)

    # Relationships
    datasource = relationship("DataSource", back_populates="tables")
    connection_table = relationship("ConnectionTable", back_populates="domain_tables")
    table_stats = relationship(
        "TableStats",
        back_populates="datasource_table",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )
    usage_events = relationship(
        "TableUsageEvent",
        back_populates="datasource_table",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )
    feedback_events = relationship(
        "TableFeedbackEvent",
        back_populates="datasource_table",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )

    def to_prompt_table(self) -> Table:
        """Convert to prompt formatter Table model.
        
        Uses ConnectionTable schema if available, otherwise falls back to legacy fields.
        """
        # Use ConnectionTable if available (new architecture)
        if self.connection_table:
            table = self.connection_table.to_prompt_table()
            # Override with domain-specific metadata if present
            if self.metadata_json:
                table.metadata_json = self.metadata_json
            return table
        
        # Legacy: use fields on DataSourceTable directly
        columns_data = self.columns or []
        pks_data = self.pks or []
        fks_data = self.fks or []
        
        columns = [
            TableColumn(name=col['name'], dtype=col.get('dtype'))
            for col in columns_data
        ]
        
        pks = [
            TableColumn(name=pk['name'], dtype=pk.get('dtype'))
            for pk in pks_data
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
            for fk in fks_data
        ]

        return Table(
            name=self.name,
            columns=columns,
            pks=pks,
            fks=fks,
            metadata_json=self.metadata_json
        )