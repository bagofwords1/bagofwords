"""Slim context snapshots keep usage summaries, never the full schema or
instruction sections (the shape the Context Browser and instruction
feedback read)."""

import json
from types import SimpleNamespace

import pytest

from app.ai.agent_v2 import AgentV2
from app.ai.context.context_view import ContextView, StaticSections, WarmSections
from app.ai.context.sections.instructions_section import InstructionItem, InstructionsSection
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import Table as PromptTable, TableColumn as PromptTableColumn
from app.schemas.data_source_schema import DataSourceSummarySchema


def _schemas(n_tables):
    tables = [
        PromptTable(name=f"t{i}", columns=[PromptTableColumn(name="id", dtype="int")], pks=[], fks=[])
        for i in range(n_tables)
    ]
    return TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds1", name="Store", type="sqlite"), tables=tables)])


@pytest.mark.parametrize("n_tables, n_instr", [(3, 0), (25, 4)])
def test_slim_snapshot_keeps_usage_not_full_sections(n_tables, n_instr):
    items = [InstructionItem(id=f"i{i}", text=f"rule {i}", category="general") for i in range(n_instr)]
    view = ContextView(
        static=StaticSections(schemas=_schemas(n_tables), instructions=InstructionsSection(items=items) if items else None),
        warm=WarmSections(),
    )
    data = json.loads(json.dumps(AgentV2._build_slim_context_snapshot(SimpleNamespace(), view, top_k_schema=10), default=str))

    assert data["static"]["schemas"] is None
    usage = data["schemas_usage"]["data_sources"][0]
    assert usage["ds_id"] == "ds1" and usage["tables_total"] == n_tables
    assert len(usage["tables_used"]) == min(n_tables, 10)
    if n_instr:
        assert data["static"]["instructions"] is None
        assert [i["id"] for i in data["instructions_usage"]] == [f"i{i}" for i in range(n_instr)]
    else:
        assert "instructions_usage" not in data
    assert "warm" in data and "meta" in data


def test_slim_snapshot_of_empty_view():
    view = ContextView(static=StaticSections(), warm=WarmSections())
    data = AgentV2._build_slim_context_snapshot(SimpleNamespace(), view)
    assert data["static"]["schemas"] is None and "schemas_usage" not in data
