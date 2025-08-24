from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from .create_data_model import (
    DataModel,
    DataModelColumn,
    SeriesItem,
)


class DataModelPatch(BaseModel):
    """Optional high-level data_model changes.
    Based on planner.py modify_widget: can change type and/or series only.
    """

    type: Optional[DataModel.__fields__["type"].annotation] = Field(
        default=None, description="Change visualization/data type"
    )
    series: Optional[List[SeriesItem]] = Field(
        default=None, description="Replace chart series configuration"
    )


class ModifyDataModelInput(BaseModel):
    """Input for applying a patch/diff to the current data model (planner modify structure)."""

    data_model: Optional[DataModelPatch] = Field(
        default=None,
        description="Optional changes to top-level data_model (type/series)",
    )
    remove_columns: Optional[List[str]] = Field(
        default=None, description="List of generated_column_name to remove"
    )
    add_columns: Optional[List[DataModelColumn]] = Field(
        default=None, description="Columns to add (full column objects)"
    )
    transform_columns: Optional[List[DataModelColumn]] = Field(
        default=None, description="Columns to transform (full replacement by name)"
    )


class ModifyDataModelOutput(BaseModel):
    """Output with the updated data model after applying modifications."""

    data_model: DataModel = Field(
        ..., description="Updated data model after applying modifications"
    )


