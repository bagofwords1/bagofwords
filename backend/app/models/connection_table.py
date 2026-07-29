from sqlalchemy import (
    Column, String, ForeignKey, JSON, Integer, Float, DateTime, Boolean,
    BigInteger, Text,
)
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey as PromptForeignKey


# `kind` values. Deliberately NOT "view": a view implies non-materialized, which
# is the opposite of what a custom query is — it is always materialized to a
# local artifact and served from there.
KIND_TABLE = "table"   # introspected from the source catalog
KIND_BOW = "bow"       # BOW-managed custom query, materialized to an artifact


class ConnectionTable(BaseSchema):
    """
    Represents a table discovered from a database connection.
    Stores the actual schema information (columns, PKs, FKs, metrics).
    DataSourceTable (DomainTable) references this to enable per-domain table activation.

    Also hosts BOW-managed **custom queries** (``kind='bow'``): admin-authored SQL
    that is materialized to an encrypted local DuckDB artifact on a schedule and
    served to agents from there instead of the source. Reusing this model (rather
    than a parallel one) means agent activation via ``DataSourceTable``, per-user
    overlays, ``table_stats`` and schema-context rendering all work unchanged.
    """
    __tablename__ = 'connection_tables'

    # NOTE: (connection_id, name) uniqueness is enforced in the service layer
    # (custom_query_service), not by a DB constraint — existing installs may
    # already hold duplicate introspected rows and a migration that added the
    # constraint would fail on them.
    name = Column(String, nullable=False)
    connection_id = Column(String(36), ForeignKey('connections.id'), nullable=False, index=True)

    # 'table' (introspected) | 'bow' (custom query). See KIND_* above.
    kind = Column(String, nullable=False, default=KIND_TABLE, server_default=KIND_TABLE)

    # --- kind='bow' only -----------------------------------------------------
    # SQL in the SOURCE dialect, run at refresh time to build the artifact. The
    # agent never sees or runs this; it queries the materialized result in
    # DuckDB SQL.
    definition_sql = Column(Text, nullable=True)
    description = Column(Text, nullable=True)

    # Refresh schedule. Field names mirror the reindex_* block on Connection so
    # the UI and scheduler patterns carry over.
    refresh_schedule_mode = Column(String, nullable=True)      # 'interval' | 'time'
    refresh_interval_minutes = Column(Integer, nullable=True)
    refresh_at_time = Column(String, nullable=True)            # "HH:MM" (24h)

    last_refreshed_at = Column(DateTime, nullable=True)
    last_refresh_status = Column(String, nullable=True)        # ok | error | running
    last_refresh_error = Column(Text, nullable=True)
    last_refresh_ms = Column(Integer, nullable=True)

    # Opaque uuid filename (never a content hash — paths must not be derivable
    # from inside the code sandbox) plus its per-artifact DuckDB encryption key,
    # Fernet-encrypted with settings.bow_config.encryption_key.
    artifact_path = Column(String, nullable=True)
    artifact_key_enc = Column(Text, nullable=True)
    artifact_bytes = Column(BigInteger, nullable=True)

    # --- row-level security (kind='bow' only) -------------------------------
    # A materialized relation holds every row the shared credential could see,
    # so RLS filters at read time against who is asking. Enforcement builds a
    # filtered view in the per-session DuckDB catalog and never registers the
    # raw relation — see app/data_sources/fast/rls.py.
    rls_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    rls_mode = Column(String, nullable=True)          # 'attribute' | 'sql' (phase 2)
    rls_policy = Column(JSON, nullable=True)
    # Unresolved identity → no rows. Starts closed: the failure mode of the
    # opposite default is handing over the whole table.
    rls_default_deny = Column(Boolean, nullable=False, default=True, server_default="1")

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
            TableColumn(
                name=col['name'],
                dtype=col.get('dtype'),
                description=col.get('description'),
                metadata=col.get('metadata'),
            )
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

        # Extract table description from metadata_json if present
        table_description = None
        if self.metadata_json and isinstance(self.metadata_json, dict):
            table_description = self.metadata_json.get('description')
        if not table_description and self.description:
            table_description = self.description

        return Table(
            name=self.name,
            description=table_description,
            columns=columns,
            pks=pks,
            fks=fks,
            metadata_json=self.metadata_json
        )

