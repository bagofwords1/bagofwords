from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel, Field


class ColumnSchema(BaseModel):
    """Schema for a column in a DBT resource"""
    name: str
    description: Optional[str] = ""
    data_type: Optional[str] = ""
    tests: Optional[List[Union[str, Dict[str, Any]]]] = Field(default_factory=list)
    meta: Optional[Dict[str, Any]] = Field(default_factory=dict)


class MetricSchema(BaseModel):
    """Schema for a DBT metric"""
    name: str
    path: str
    type: str = "metric"
    description: Optional[str] = ""
    label: Optional[str] = ""
    calculation_method: Optional[str] = ""
    expression: Optional[str] = ""
    timestamp: Optional[str] = ""
    time_grains: Optional[List[str]] = Field(default_factory=list)
    dimensions: Optional[List[str]] = Field(default_factory=list)
    filters: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    meta: Optional[Dict[str, Any]] = Field(default_factory=dict)
    model: Optional[str] = ""
    sql: Optional[str] = ""
    window: Optional[Dict[str, Any]] = Field(default_factory=dict)
    tags: Optional[List[str]] = Field(default_factory=list)
    refs: Optional[List[str]] = Field(default_factory=list)
    depends_on: Optional[List[str]] = Field(default_factory=list)
    columns: Optional[List[ColumnSchema]] = Field(default_factory=list)


class SourceSchema(BaseModel):
    """Schema for a DBT source"""
    name: str
    path: str
    type: str = "source"
    description: Optional[str] = ""
    database: Optional[str] = ""
    schema: Optional[str] = ""
    loader: Optional[str] = ""
    freshness: Optional[Dict[str, Any]] = Field(default_factory=dict)
    meta: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ModelSchema(BaseModel):
    """Schema for a DBT model"""
    name: str
    path: str
    type: str  # model_config or model_sql
    description: Optional[str] = ""
    config: Optional[Dict[str, Any]] = Field(default_factory=dict)
    meta: Optional[Dict[str, Any]] = Field(default_factory=dict)
    sql_content: Optional[str] = ""  # Only for model_sql type


class MacroSchema(BaseModel):
    """Schema for a DBT macro"""
    name: str
    path: str
    type: str = "macro"


class SeedSchema(BaseModel):
    """Schema for a DBT seed"""
    name: str
    path: str
    type: str = "seed"


class TestSchema(BaseModel):
    """Schema for a DBT test"""
    name: str
    path: str
    type: str = "singular_test"
    description: Optional[str] = ""
    sql_content: Optional[str] = ""


class ExposureSchema(BaseModel):
    """Schema for a DBT exposure"""
    name: str
    path: str
    type: str = "exposure"
    description: Optional[str] = ""
    maturity: Optional[str] = ""
    url: Optional[str] = ""
    depends_on: Optional[List[str]] = Field(default_factory=list)
    owner: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ProjectConfigSchema(BaseModel):
    """Schema for DBT project configuration"""
    name: str
    version: str
    config_version: int = Field(..., alias="config-version")
    profile: str
    model_paths: List[str] = Field(..., alias="model-paths")
    seed_paths: List[str] = Field(..., alias="seed-paths")
    test_paths: List[str] = Field(..., alias="test-paths")
    metric_paths: List[str] = Field(..., alias="metric-paths")
    analysis_paths: List[str] = Field(..., alias="analysis-paths")
    macro_paths: List[str] = Field(..., alias="macro-paths")
    target_path: str = Field(..., alias="target-path")
    clean_targets: List[str] = Field(..., alias="clean-targets")
    models: Dict[str, Any]
    metrics: Optional[Dict[str, Any]] = None


class DBTProjectResourcesSchema(BaseModel):
    """Schema for all DBT resources extracted from a project"""
    metrics: List[MetricSchema] = Field(default_factory=list)
    models: List[ModelSchema] = Field(default_factory=list)
    sources: List[SourceSchema] = Field(default_factory=list)
    seeds: List[SeedSchema] = Field(default_factory=list)
    macros: List[MacroSchema] = Field(default_factory=list)
    tests: List[TestSchema] = Field(default_factory=list)
    exposures: List[ExposureSchema] = Field(default_factory=list)
    columns_by_resource: Dict[str, List[ColumnSchema]] = Field(default_factory=dict)
    docs_by_resource: Dict[str, str] = Field(default_factory=dict)
    project_config: Optional[ProjectConfigSchema] = None


class ResourceSummarySchema(BaseModel):
    """Schema for a summary of DBT resources"""
    metrics: int = 0
    models: int = 0
    sources: int = 0
    seeds: int = 0
    macros: int = 0
    tests: int = 0
    exposures: int = 0


# --- SQLX / Dataform schemas -------------------------------------------------


class SQLXColumnSchema(BaseModel):
    """Schema for a column in a SQLX (Dataform) resource."""

    name: str
    description: Optional[str] = ""
    data_type: Optional[str] = ""
    meta: Optional[Dict[str, Any]] = Field(default_factory=dict)


class SQLXTableSchema(BaseModel):
    """Schema for a SQLX table, view or incremental action."""

    name: str
    path: str
    type: str = "sqlx_table"

    # High-level configuration
    materialization: Optional[str] = ""  # table / view / incremental
    description: Optional[str] = ""
    tags: List[str] = Field(default_factory=list)
    schema_expr: Optional[str] = ""  # Expression used for schema/database
    unique_key: List[str] = Field(default_factory=list)

    # Warehouse-specific configuration (kept flat for now)
    partition_by: Optional[str] = ""
    cluster_by: List[str] = Field(default_factory=list)

    # Assertions and other config snippets stored as-is
    assertions: Dict[str, Any] = Field(default_factory=dict)

    # SQL body and pre-operations
    sql_body: Optional[str] = ""
    pre_operations_raw: Optional[str] = ""
    sqlx_source_snippet: Optional[str] = ""

    # Columns and dependencies
    columns: List[SQLXColumnSchema] = Field(default_factory=list)
    depends_on: List[str] = Field(default_factory=list)

    # Raw config block as a best-effort parsed object or string
    raw_config: Dict[str, Any] = Field(default_factory=dict)


class SQLXAssertionSchema(BaseModel):
    """Schema for a SQLX assertion action."""

    name: str
    path: str
    type: str = "sqlx_assertion"
    description: Optional[str] = ""
    tags: List[str] = Field(default_factory=list)
    sql_body: Optional[str] = ""
    depends_on: List[str] = Field(default_factory=list)
    raw_config: Dict[str, Any] = Field(default_factory=dict)


class SQLXOperationSchema(BaseModel):
    """Schema for a SQLX operation action."""

    name: str
    path: str
    type: str = "sqlx_operation"
    description: Optional[str] = ""
    tags: List[str] = Field(default_factory=list)
    sql_body: Optional[str] = ""
    depends_on: List[str] = Field(default_factory=list)
    raw_config: Dict[str, Any] = Field(default_factory=dict)


class SQLXDeclarationSchema(BaseModel):
    """Schema for a SQLX declaration (external table/view)."""

    name: str
    path: str
    type: str = "sqlx_declaration"
    description: Optional[str] = ""
    database: Optional[str] = ""
    schema: Optional[str] = ""
    raw_config: Dict[str, Any] = Field(default_factory=dict)


class SQLXProjectResourcesSchema(BaseModel):
    """Schema for all SQLX resources extracted from a project."""

    tables: List[SQLXTableSchema] = Field(default_factory=list)
    assertions: List[SQLXAssertionSchema] = Field(default_factory=list)
    operations: List[SQLXOperationSchema] = Field(default_factory=list)
    declarations: List[SQLXDeclarationSchema] = Field(default_factory=list)

    columns_by_resource: Dict[str, List[SQLXColumnSchema]] = Field(default_factory=dict)
    docs_by_resource: Dict[str, str] = Field(default_factory=dict)

    # For now we keep project config untyped; can be expanded later.
    project_config: Optional[Dict[str, Any]] = None
