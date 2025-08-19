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
    
    class Config:
        from_attributes = True

    def to_prompt_table(self) -> 'Table':
        """Convert to prompt formatter Table model."""
        from app.ai.prompt_formatters import Table, TableColumn, ForeignKey

        return Table(
            name=self.name,
            columns=[TableColumn(name=col.name, dtype=col.dtype) for col in self.columns],
            pks=[TableColumn(name=pk.name, dtype=pk.dtype) for pk in self.pks],
            fks=[
                ForeignKey(
                    column=TableColumn(name=fk.column.name, dtype=fk.column.dtype),
                    references_name=fk.references_name,
                    references_column=TableColumn(
                        name=fk.references_column.name,
                        dtype=fk.references_column.dtype
                    )
                )
                for fk in self.fks
            ]
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
    
    class Config:
        from_attributes = True
