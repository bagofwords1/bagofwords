"""Generated demo dataset — the spec the agent designs and the user approves.

The spec is declarative: tables, columns, keys, row counts, plain-text
generation hints and suggested agents. It never carries code or a filesystem
path. Rows are produced afterwards, table by table, by small-model generated
pandas/numpy code that runs in the code sandbox and is validated against this
spec before anything is written (see app/services/demo_data/generator.py).

Domain-agnostic on purpose: the same shape covers transactional data (sales,
finance), entities with history (HR, CRM), event/log streams, metrics time
series and machine/IoT readings.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field, field_validator

ColumnType = Literal["integer", "real", "text", "date", "datetime", "boolean", "json"]

IDENT_RE = r"^[a-z][a-z0-9_]{0,62}$"

# Size guardrails. Total rows bound generation time and the SQLite file size;
# the per-table bound lets a metrics/log table be large while entities stay small.
MAX_TABLES = 15
MAX_COLUMNS = 40
MAX_ROWS_PER_TABLE = 300_000
MAX_TOTAL_ROWS = 1_000_000
MAX_AGENTS = 4


class DemoColumn(BaseModel):
    name: str = Field(..., pattern=IDENT_RE, description="snake_case column name.")
    type: ColumnType = Field(..., description="integer | real | text | date | datetime | boolean | json")
    description: str = Field("", max_length=300, description="Business meaning of the column.")
    primary_key: bool = Field(False, description="True for the table's primary key column.")
    references: Optional[str] = Field(
        None,
        description="Foreign key target as 'table.column' (must be that table's primary key).",
    )
    nullable: bool = Field(False, description="True when NULLs are realistic for this column.")
    generation_hint: Optional[str] = Field(
        None,
        max_length=400,
        description=(
            "How values should look/behave, e.g. 'lognormal around $60, Q4 +40%', "
            "'80% card / 15% paypal / 5% bnpl', '= quantity * unit_price', "
            "'INFO 85% / WARN 10% / ERROR 5%'."
        ),
    )

    @field_validator("references")
    @classmethod
    def _ref_shape(cls, v):
        if v is None or v == "":
            return None
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}", v):
            raise ValueError("references must look like 'table.column'")
        return v


class DemoTable(BaseModel):
    name: str = Field(..., pattern=IDENT_RE, description="snake_case table name.")
    description: str = Field(..., max_length=500, description="What one row represents.")
    row_count: int = Field(..., ge=1, le=MAX_ROWS_PER_TABLE, description="Target number of rows.")
    columns: List[DemoColumn] = Field(..., min_length=1, max_length=MAX_COLUMNS)


class DemoAgentSuggestion(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Agent name, unique per organization.")
    description: str = Field("", max_length=1000)
    icon: Optional[str] = Field(
        None,
        max_length=16,
        description="ONE emoji that represents the agent's focus (e.g. 💰, 👥, 📈, 🚨, 🏭).",
    )
    tables: List[str] = Field(..., min_length=1, description="Active tables for this agent; must exist in the spec.")
    conversation_starters: List[str] = Field(default_factory=list, max_length=6)
    instructions: List[str] = Field(
        default_factory=list,
        max_length=10,
        description="Business definitions/rules scoped to this agent, e.g. 'Net revenue = gross - refunds'.",
    )
    selected: bool = Field(True, description="Default checkbox state on the confirmation card.")


class DemoDatasetSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Connection name, e.g. 'Demo – E-commerce Finance'.")
    domain: str = Field(..., min_length=1, max_length=300, description="The user's ask, e.g. 'finance in e-commerce'.")
    description: str = Field("", max_length=2000)
    icon: Optional[str] = Field(None, max_length=16, description="ONE emoji for the dataset/connection (e.g. 🛒).")
    date_range_start: date = Field(..., description="ISO date; time-based data starts here.")
    date_range_end: date = Field(..., description="ISO date; must not be in the future.")
    tables: List[DemoTable] = Field(..., min_length=1, max_length=MAX_TABLES)
    realism_notes: List[str] = Field(
        default_factory=list,
        max_length=15,
        description=(
            "Cross-table behaviors the data must honor, e.g. 'refunds only for delivered "
            "orders, ~3%, within 30 days', 'inject 2 incidents: p99 latency > 2s for 30-60 min "
            "with matching ERROR log bursts'."
        ),
    )
    agents: List[DemoAgentSuggestion] = Field(default_factory=list, max_length=MAX_AGENTS)
    seed: int = Field(42, description="Generation seed for reproducibility.")


def table_map(spec: DemoDatasetSpec) -> Dict[str, DemoTable]:
    return {t.name: t for t in spec.tables}


def primary_key(table: DemoTable) -> Optional[DemoColumn]:
    for c in table.columns:
        if c.primary_key:
            return c
    return None


def validate_spec(spec: DemoDatasetSpec, *, today: Optional[date] = None) -> List[str]:
    """Structural checks a pydantic field can't express. Returns error strings
    (empty = valid). Written to be read by the model so it can fix and retry."""
    errors: List[str] = []
    today = today or date.today()
    tables = table_map(spec)

    if len(tables) != len(spec.tables):
        errors.append("Table names must be unique.")
    if spec.date_range_start > spec.date_range_end:
        errors.append("date_range_start must be on or before date_range_end.")
    if spec.date_range_end > today:
        errors.append(f"date_range_end ({spec.date_range_end}) is in the future; use {today} or earlier.")

    total = sum(t.row_count for t in spec.tables)
    if total > MAX_TOTAL_ROWS:
        errors.append(f"Total rows {total:,} exceeds the {MAX_TOTAL_ROWS:,} limit; lower row_count values.")

    for t in spec.tables:
        names = [c.name for c in t.columns]
        if len(set(names)) != len(names):
            errors.append(f"{t.name}: column names must be unique.")
        pks = [c for c in t.columns if c.primary_key]
        if len(pks) > 1:
            errors.append(f"{t.name}: at most one primary_key column (use a surrogate id).")
        for c in t.columns:
            if c.primary_key and c.nullable:
                errors.append(f"{t.name}.{c.name}: a primary key cannot be nullable.")
            if not c.references:
                continue
            ref_table, ref_col = c.references.split(".", 1)
            target = tables.get(ref_table)
            if target is None:
                errors.append(f"{t.name}.{c.name}: references unknown table '{ref_table}'.")
                continue
            tpk = primary_key(target)
            if tpk is None or tpk.name != ref_col:
                errors.append(
                    f"{t.name}.{c.name}: references '{c.references}', which is not "
                    f"{ref_table}'s primary key."
                )

    if not errors:
        try:
            generation_order(spec)
        except ValueError as e:
            errors.append(str(e))

    agent_names: Set[str] = set()
    for a in spec.agents:
        key = a.name.strip().lower()
        if key in agent_names:
            errors.append(f"Agent names must be unique ('{a.name}').")
        agent_names.add(key)
        missing = [x for x in a.tables if x not in tables]
        if missing:
            errors.append(f"Agent '{a.name}' lists unknown tables: {', '.join(missing)}.")
    return errors


def generation_order(spec: DemoDatasetSpec) -> List[str]:
    """Parents before children (self-references allowed). Raises on cycles."""
    deps: Dict[str, Set[str]] = {}
    for t in spec.tables:
        deps[t.name] = {
            c.references.split(".", 1)[0]
            for c in t.columns
            if c.references and c.references.split(".", 1)[0] != t.name
        }
    order: List[str] = []
    remaining = dict(deps)
    # Stable: keep the spec's order among tables that are ready together.
    spec_order = [t.name for t in spec.tables]
    while remaining:
        ready = [n for n in spec_order if n in remaining and not (remaining[n] - set(order))]
        if not ready:
            raise ValueError(
                "Foreign keys form a cycle between: " + ", ".join(sorted(remaining))
                + ". Break it (e.g. drop one reference or move it to a link table)."
            )
        for n in ready:
            order.append(n)
            remaining.pop(n)
    return order
