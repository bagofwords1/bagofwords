"""<mcp_tools> shows exactly the MCP tools the run can call, and how.

Native tools carry their schema in the request's tools array, so the block is
an index. When the run could not build its MCP catalog it falls back to
execute_mcp: the block then inlines schemas and says to call execute_mcp.
The context hub applies the run's catalog to the block, so a tool the
registry left out (disabled or denied under the strictest setting across the
run's agents, or MCP not offered at all) is never listed, and policies match.
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
        connection_name="SampleMCP",
        policy="allow",
        input_schema={
            "type": "object",
            "properties": {"contract_id": {"type": "string"}},
            "required": ["contract_id"],
        },
    )


def _section(tool_count, *, native_mcp):
    return TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds-1", name="SampleAgent", type="mcp"),
        tables=[],
        mcp_tools=[_tool(f"get_thing_{i}") for i in range(tool_count)],
        native_mcp=native_mcp,
    )


def _hub(*sections):
    from app.ai.context.context_hub import ContextHub

    hub = ContextHub.__new__(ContextHub)
    hub._static_cache = {"schemas": TablesSchemaContext(data_sources=list(sections))}
    return hub


def test_native_path_omits_the_inline_schema():
    """Native registration already sends the schema; inlining would duplicate it."""
    xml = _section(3, native_mcp=True)._render_mcp_tools_xml()
    assert "contract_id" not in xml
    assert "get_thing_0" in xml
    assert "execute_mcp" not in xml


def test_sections_default_to_native_tools():
    section = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds-1", name="SampleAgent", type="mcp"),
        mcp_tools=[_tool("get_thing")],
    )
    assert section.native_mcp is True


def test_catalog_failure_falls_back_to_execute_mcp_with_inline_schemas():
    section = _section(3, native_mcp=True)
    assert _hub(section).set_mcp_catalog(None, gateway=True) is True
    xml = section._render_mcp_tools_xml()
    assert "execute_mcp" in xml and 'id="conn-1"' in xml
    assert "contract_id" in xml, "the fallback has no other copy of the schema"
    assert "in your tool list as mcp__" not in xml


def test_tools_past_the_cap_are_marked_and_the_note_says_to_search_for_them():
    """A tool past tools_max is not in the request's tool list. Listed without a
    marker, the model called it by a guessed mcp__ name with guessed arguments."""
    section = _section(3, native_mcp=True)
    hub = _hub(section)
    hub.set_mcp_catalog({
        ("conn-1", "get_thing_0"): {"deferred": False, "policy": "allow"},
        ("conn-1", "get_thing_1"): {"deferred": False, "policy": "allow"},
        ("conn-1", "get_thing_2"): {"deferred": True, "policy": "allow"},
    })

    xml = section._render_mcp_tools_xml()
    assert '<tool name="get_thing_2" deferred="true"' in xml
    assert '<tool name="get_thing_0">' in xml
    assert "search_tools" in xml and "call_tool" in xml


def test_the_block_lists_only_what_the_registry_registered_with_its_policy():
    """Two agents share a connection; one disables get_thing_1 and the other
    asks before get_thing_0. Each agent's own view would list get_thing_1 as
    callable; the registry (strictest across agents) did not register it."""
    a, b = _section(3, native_mcp=True), _section(3, native_mcp=True)
    _hub(a, b).set_mcp_catalog({
        ("conn-1", "get_thing_0"): {"deferred": False, "policy": "ask"},
        ("conn-1", "get_thing_2"): {"deferred": False, "policy": "allow"},
    })
    for section in (a, b):
        xml = section._render_mcp_tools_xml()
        assert "get_thing_1" not in xml
        assert '<tool name="get_thing_0" policy="ask">' in xml
        assert '<tool name="get_thing_2">' in xml


def test_mcp_not_offered_hides_every_tool():
    section = _section(3, native_mcp=True)
    assert _hub(section).set_mcp_catalog({}) is True
    assert section.mcp_tools == []


def test_applying_the_same_catalog_again_reports_no_change():
    section = _section(2, native_mcp=True)
    hub = _hub(section)
    view = {("conn-1", f"get_thing_{i}"): {"deferred": False, "policy": "allow"} for i in range(2)}
    assert hub.set_mcp_catalog(view) is False
    assert hub.set_mcp_catalog(view) is False
