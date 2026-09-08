"""Versioned, bounded public query language for the built-in training source."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOURCE_ID = "builtin:bow"
CLIENT_KEY = "bow"
MAX_ROWS = 10_000
MAX_GROUPS = 1_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BowTimeRange(StrictModel):
    relative: str | None = None
    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.relative is not None:
            import re
            if self.start or self.end or not re.fullmatch(r"[1-9][0-9]{0,3}[hd]", self.relative):
                raise ValueError("Use relative hours/days OR explicit start and end")
        elif self.start is None or self.end is None:
            raise ValueError("Supply relative or both start and end")
        return self


class BowMetric(StrictModel):
    op: Literal["count", "count_distinct", "sum", "avg"]
    field: str | None = None
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class BowSort(StrictModel):
    field: str
    direction: Literal["asc", "desc"] = "desc"


class BowQuery(StrictModel):
    dataset: Literal["runs", "tool_calls"]
    query: str = Field(default="", max_length=8000)
    time_range: BowTimeRange = Field(default_factory=lambda: BowTimeRange(relative="30d"))
    tz_offset_minutes: int = Field(default=0, ge=-840, le=840)
    columns: list[str] | None = None
    group_by: list[str] = Field(default_factory=list, max_length=8)
    metrics: list[BowMetric] = Field(default_factory=list, max_length=16)
    sort: list[BowSort] = Field(default_factory=list, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=MAX_ROWS)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.group_by and not self.metrics:
            raise ValueError("group_by requires metrics")
        if self.columns is not None and (not self.columns or self.metrics):
            raise ValueError("columns is for row queries; aggregates return group_by and metrics")
        return self
