"""Where an MCP tool's argument schema lives must be decided exactly once.

Native registration puts each tool's schema in the request's ``tools`` array;
the ``<mcp_tools>`` block inlines it instead when the gateway path serves. The
two must never both skip it — that strands the agent on a ``search_mcps`` round
trip — and ideally never both carry it, which pays for the same bytes twice.

The renderer therefore reads a decision the builder resolved report-wide,
rather than recomputing one from its own per-agent view.
"""
import pytest

from app.ai.context.sections.tables_schema_section import (
    MCPToolItem, TablesSchemaContext,
)
from app.schemas.data_source_schema import DataSourceSummarySchema


def _tool(name):
    return MCPToolItem(
        name=name,
        description=f"does {name}",
        connection_id="conn-1",
        connection_name="LNMCP",
        policy="allow",
        input_schema={
            "type": "object",
            "properties": {"contract_id": {"type": "string"}},
            "required": ["contract_id"],
        },
    )


def _section(tool_count, *, native_mcp):
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds-1", name="LNTesting", type="mcp"),
        tables=[],
        mcp_tools=[_tool(f"get_thing_{i}") for i in range(tool_count)],
        native_mcp=native_mcp,
    )


def test_gateway_path_inlines_the_schema():
    xml = _section(3, native_mcp=False)._render_mcp_tools_xml()
    assert "contract_id" in xml, "gateway path must carry the schema inline"


def test_native_path_omits_the_inline_schema():
    """Native registration already sends the schema; inlining would duplicate it."""
    xml = _section(3, native_mcp=True)._render_mcp_tools_xml()
    assert "contract_id" not in xml
    # The tools themselves are still advertised, just without argument shapes.
    assert "get_thing_0" in xml


def test_tools_are_always_listed_under_either_path():
    for native in (True, False):
        xml = _section(3, native_mcp=native)._render_mcp_tools_xml()
        for i in range(3):
            assert f"get_thing_{i}" in xml, f"tool missing (native={native})"


def test_default_is_the_safe_direction():
    """A section built without the flag inlines — a schema present, never absent."""
    section = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds-1", name="LNTesting", type="mcp"),
        mcp_tools=[_tool("get_thing")],
    )
    assert section.native_mcp is False
    assert "contract_id" in section._render_mcp_tools_xml()


def _builder(organization_settings=None):
    import app.models  # noqa: F401  (register mappers before touching models)
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder

    return SchemaContextBuilder(
        db=None, data_sources=[], organization=None, report=None,
        organization_settings=organization_settings,
    )


def test_builder_counts_across_every_agent_in_the_report(monkeypatch):
    """Two agents of 8 tools each is a 16-tool report: over the 12 threshold.

    Counting per agent would see 8, stay on the gateway, and disagree with the
    planner — which registers natively across every agent the report has.
    """
    monkeypatch.delenv("BOW_MCP_NATIVE_TOOLS", raising=False)
    seen = {}

    def _fake_enabled(settings, count):
        seen["count"] = count
        return count >= 12

    monkeypatch.setattr(
        "app.ai.tools.mcp_tool_registry.native_tools_enabled", _fake_enabled
    )

    sections = [_section(8, native_mcp=False), _section(8, native_mcp=False)]
    assert _builder(object())._apply_native_mcp_decision(sections) is True
    assert seen["count"] == 16
    assert all(s.native_mcp is True for s in sections)


def test_builder_stays_on_gateway_below_the_threshold(monkeypatch):
    monkeypatch.delenv("BOW_MCP_NATIVE_TOOLS", raising=False)
    sections = [_section(4, native_mcp=True)]  # seeded wrong on purpose
    # Real settings, real defaults: 4 tools is under the threshold of 12.
    import app.models  # noqa: F401
    from app.models.organization_settings import OrganizationSettings

    settings = OrganizationSettings(organization_id="org", config={})
    assert _builder(settings)._apply_native_mcp_decision(sections) is False
    assert sections[0].native_mcp is False
    assert "contract_id" in sections[0]._render_mcp_tools_xml()


def test_builder_falls_back_to_inline_when_resolution_raises(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("app.ai.tools.mcp_tool_registry.native_tools_enabled", _boom)
    sections = [_section(30, native_mcp=True)]
    assert _builder(object())._apply_native_mcp_decision(sections) is False
    assert "contract_id" in sections[0]._render_mcp_tools_xml()
