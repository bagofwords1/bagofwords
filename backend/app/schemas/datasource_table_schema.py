from pydantic import BaseModel, validator
from typing import Optional, List, Dict, Any, Union


class TableColumnSchema(BaseModel):
    name: str
    dtype: Optional[str] = None
    is_active: bool = True
    
    class Config:
        from_attributes = True


class ForeignKeySchema(BaseModel):
    column: TableColumnSchema
    references_name: str
    references_column: TableColumnSchema
    
    class Config:
        from_attributes = True


class DataSourceTableSchema(BaseModel):
    id: Optional[str] = None
    name: str
    columns: List[Dict[str, Any]]  # Keep as raw JSON
    no_rows: int = 0
    datasource_id: str
    pks: List[Dict[str, Any]]  # Keep as raw JSON
    fks: List[Dict[str, Any]]  # Keep as raw JSON
    is_active: bool = False
    metadata_json: Optional[Dict[str, Any]] = None
    # Connection info (for multi-connection support)
    connection_id: Optional[str] = None
    connection_name: Optional[str] = None
    connection_type: Optional[str] = None
    # Topology and richness metrics
    centrality_score: Optional[float] = None
    richness: Optional[float] = None
    degree_in: Optional[int] = None
    degree_out: Optional[int] = None
    entity_like: Optional[bool] = None
    metrics_computed_at: Optional[str] = None
    # Usage stats (populated when with_stats=True)
    usage_count: Optional[int] = None
    success_count: Optional[int] = None
    failure_count: Optional[int] = None
    pos_feedback_count: Optional[int] = None
    neg_feedback_count: Optional[int] = None
    
    class Config:
        from_attributes = True

    def to_prompt_table(self) -> 'Table':
        """Convert to prompt formatter Table model."""
        from app.ai.prompt_formatters import Table, TableColumn, ForeignKey
        # columns/pks/fks are dict-shaped in this schema; normalize to prompt models
        cols = [
            TableColumn(name=(col.get('name') if isinstance(col, dict) else getattr(col, 'name', '')),
                        dtype=(col.get('dtype') if isinstance(col, dict) else getattr(col, 'dtype', None)))
            for col in (self.columns or [])
        ]
        pks = [
            TableColumn(name=(pk.get('name') if isinstance(pk, dict) else getattr(pk, 'name', '')),
                        dtype=(pk.get('dtype') if isinstance(pk, dict) else getattr(pk, 'dtype', None)))
            for pk in (self.pks or [])
        ]
        fks: List[ForeignKey] = []
        for fk in (self.fks or []):
            if isinstance(fk, dict):
                col_d = fk.get('column') or {}
                ref_col_d = fk.get('references_column') or {}
                fks.append(
                    ForeignKey(
                        column=TableColumn(name=col_d.get('name'), dtype=col_d.get('dtype')),
                        references_name=fk.get('references_name'),
                        references_column=TableColumn(name=ref_col_d.get('name'), dtype=ref_col_d.get('dtype')),
                    )
                )
            else:
                # Best-effort fallback if objects leak through
                fks.append(
                    ForeignKey(
                        column=TableColumn(name=getattr(getattr(fk, 'column', None), 'name', ''), dtype=getattr(getattr(fk, 'column', None), 'dtype', None)),
                        references_name=getattr(fk, 'references_name', ''),
                        references_column=TableColumn(name=getattr(getattr(fk, 'references_column', None), 'name', ''), dtype=getattr(getattr(fk, 'references_column', None), 'dtype', None)),
                    )
                )

        return Table(
            name=self.name,
            columns=cols,
            pks=pks,
            fks=fks,
            metadata_json=self.metadata_json,
        )


class DataSourceTableCreateSchema(DataSourceTableSchema):
    """Schema for creating a new DataSourceTable."""
    pass


class DataSourceTableUpdateSchema(BaseModel):
    """Schema for updating an existing DataSourceTable."""
    name: Optional[str] = None
    columns: Optional[list[TableColumnSchema]] = None
    no_rows: Optional[int] = None
    pks: Optional[list[TableColumnSchema]] = None
    fks: Optional[list[ForeignKeySchema]] = None
    is_active: Optional[bool] = None
    centrality_score: Optional[float] = None
    richness: Optional[float] = None
    degree_in: Optional[int] = None
    degree_out: Optional[int] = None
    entity_like: Optional[bool] = None
    metrics_computed_at: Optional[str] = None
    
    class Config:
        from_attributes = True


class ConnectionInfo(BaseModel):
    """Minimal connection info for filter dropdown."""
    id: str
    name: str
    type: str

    class Config:
        from_attributes = True


class PaginatedTablesResponse(BaseModel):
    """Paginated response for large table lists."""
    tables: List[DataSourceTableSchema]
    total: int  # Total tables matching current filter
    page: int
    page_size: int
    total_pages: int
    schemas: List[str]  # Distinct schema names for filter dropdown
    connections: List[ConnectionInfo]  # Connections for filter dropdown
    selected_count: int  # Count of is_active=True across ALL tables
    total_tables: int  # Total count of ALL tables for this datasource (no filters)
    has_more: bool

    class Config:
        from_attributes = True


class BulkUpdateTablesRequest(BaseModel):
    """Request schema for bulk activate/deactivate tables."""
    action: str  # "activate" or "deactivate"
    filter: Optional[Dict[str, Any]] = None  # {"schema": "...", "search": "..."}
    
    class Config:
        from_attributes = True


class DeltaUpdateTablesRequest(BaseModel):
    """Request schema for delta-based table status updates."""
    activate: List[str] = []  # Table names to set is_active=True
    deactivate: List[str] = []  # Table names to set is_active=False
    
    class Config:
        from_attributes = True


class DeltaUpdateTablesResponse(BaseModel):
    """Response schema for delta-based updates."""
    activated_count: int
    deactivated_count: int
    total_selected: int  # New total of is_active=True tables
    
    class Config:
        from_attributes = True
