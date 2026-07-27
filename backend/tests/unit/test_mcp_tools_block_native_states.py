"""The <mcp_tools> block must stay truthful about which tools are native.

Regression for a bug in the native-registration rollout: the block lists EVERY
tool (the context builder applies no budget), but only the per-connection budget
is registered natively. The first version suppressed inline schemas whenever
native mode was on and told the model "each tool above is also available as
mcp__<connection>__<tool> ... do not call search_mcps for it".

With monday (93 tools) and a 60 budget that stranded 33 tools: listed by name,
schema suppressed, no native tool, and explicitly told not to go looking. The
model cannot emit a tool name outside the provider's tools array, so it fell
back to execute_mcp and guessed arguments — with the one mechanism that would
have helped (search_mcps) disabled by our own note.
"""
import pytest

from app.ai.tools.mcp_tool_registry import native_tools_enabled, native_tools_budget


def _decide(tools_per_connection, inline_threshold=40):
    """Mirror of the renderer's decision in tables_schema_section."""
    native_on = native_tools_enabled()
    native_covers_all = native_on and all(
        n <= native_tools_budget() for n in tools_per_connection
    )
    total = sum(tools_per_connection)
    inline = (not native_covers_all) and total <= inline_threshold
    if native_covers_all:
        note = "all-native"
    elif native_on:
        note = "mixed"
    else:
        note = "gateway"
    return inline, note


def test_gateway_mode_inlines_schemas(monkeypatch):
    monkeypatch.delenv("BOW_MCP_NATIVE_TOOLS", raising=False)
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "60")
    assert _decide([12]) == (True, "gateway")


def test_all_native_suppresses_inline_schemas(monkeypatch):
    """Everything is covered natively, so inlining would pay twice."""
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "true")
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "60")
    assert _decide([12]) == (False, "all-native")


def test_mixed_catalog_keeps_schemas_and_does_not_claim_all_native(monkeypatch):
    """monday's shape: 93 tools, 60 budget. The tail must NOT be stranded."""
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "true")
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "60")
    inline, note = _decide([93])
    assert note == "mixed", "must not claim every tool is native"
    # 93 is over the inline threshold too, so the tail reaches its schema via
    # search_mcps — which the mixed note explicitly tells it to use.
    assert inline is False


def test_mixed_small_catalog_still_inlines(monkeypatch):
    """Over budget but under the inline threshold: schemas stay inline so the
    non-native tools are not left schema-less."""
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "true")
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "10")
    assert _decide([25]) == (True, "mixed")


def test_budget_is_per_connection(monkeypatch):
    """Two connections each under budget are fully covered, even though their
    combined count exceeds it — the global budget bug this replaced would have
    starved the second connection entirely."""
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "true")
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "60")
    _, note = _decide([50, 40])
    assert note == "all-native"


def test_one_oversized_connection_makes_the_catalog_mixed(monkeypatch):
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS", "true")
    monkeypatch.setenv("BOW_MCP_NATIVE_TOOLS_MAX", "60")
    _, note = _decide([93, 14])
    assert note == "mixed"
