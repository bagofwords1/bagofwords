from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from app.schemas.param_schema import ParamSpec


class AddParameterInput(BaseModel):
    """Input for add_parameter tool.

    Retro-parameterization: add ONE declared parameter to an EXISTING query
    without rebuilding it from scratch. The tool rewrites only the query's
    filtering predicate (binding the parameter to `column` with the safe
    `:name` placeholder contract), re-runs it once, and promotes the new step.
    """

    query_id: str = Field(..., description=(
        "Id of the EXISTING query to parameterize (from create_data results or the Summary panel). "
        "The query keeps its identity — visualizations bound to it pick up the parameter automatically."
    ))
    parameter: ParamSpec = Field(..., description=(
        "The parameter to add (same ParamSpec contract as create_data: name, type, label, default, "
        "required, source, options / options_source). For an enum-like dimension use options_source "
        "referencing a dimension query (by exact title or id) — never derive choices from the filtered rows."
    ))
    column: str = Field(..., description=(
        "The SQL column or expression in this query's source tables that the parameter filters, "
        "e.g. 'g.Name' or 'c.Country'. The rewrite binds it as (:name IS NULL OR col = :name) "
        "(optional param), col IN :name (list type)."
    ))


class AddParameterOutput(BaseModel):
    """Output from add_parameter tool."""

    success: bool = Field(...)
    query_id: str = Field(...)
    title: Optional[str] = Field(default=None)
    parameters: Optional[List[Dict[str, Any]]] = Field(default=None, description="Full declared parameter list after the addition")
    step_id: Optional[str] = Field(default=None)
    row_count: Optional[int] = Field(default=None)
    error: Optional[str] = Field(default=None)
