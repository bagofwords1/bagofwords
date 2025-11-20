from sqlalchemy import Column, String, ForeignKey, JSON, Integer, Float, DateTime
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey as PromptForeignKey
from sqlalchemy import Boolean

class DataSourceTable(BaseSchema):
    __tablename__ = 'datasource_tables'

    name = Column(String, nullable=False)
    columns = Column(JSON, nullable=False)
    no_rows = Column(Integer, nullable=False, default=0)
    datasource_id = Column(String(36), ForeignKey('data_sources.id'), nullable=False)

    pks = Column(JSON, nullable=False)
    fks = Column(JSON, nullable=False)

    is_active = Column(Boolean, nullable=False, default=True)

    metadata_json = Column(JSON, nullable=True)

    # Topology and richness metrics (computed on schema refresh)
    centrality_score = Column(Float, nullable=True)
    richness = Column(Float, nullable=True)
    degree_in = Column(Integer, nullable=True)
    degree_out = Column(Integer, nullable=True)
    entity_like = Column(Boolean, nullable=True)
    metrics_computed_at = Column(DateTime, nullable=True)

    datasource = relationship("DataSource", back_populates="tables")
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