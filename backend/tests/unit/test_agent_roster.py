"""Unit tests for the agent roster / focus resolution (app.ai.context.agent_roster).

Covers the pure logic paths that don't require a database:
  - roster XML rendering + focused marking,
  - usage-ranked seed selection,
  - the count gate (few agents -> render all; many -> roster + focus),
  - the description/one-liner fallback.
"""
from types import SimpleNamespace

import pytest

from app.ai.context.agent_roster import (
    RosterAgent,
    render_agent_roster_xml,
    _seed_focus,
    load_agent_one_liners,
    build_focus_and_roster,
    DEFAULT_INDEX_THRESHOLD,
)


def _ds(i, name, desc=None, status="published", primary=None):
    return SimpleNamespace(
        id=f"id{i}", name=name, description=desc, context=None,
        publish_status=status, primary_instruction_id=primary,
    )


def _section(dsid, tables=0, tools=0, files=0):
    info = SimpleNamespace(id=dsid)
    return SimpleNamespace(
        info=info,
        tables=[object()] * tables,
        mcp_tools=[object()] * tools,
        file_scopes=[object()] * files,
    )


def test_render_roster_marks_focused_and_counts():
    agents = [
        RosterAgent("a", "Sales", "Revenue and orders", 12, "tables", "published"),
        RosterAgent("b", "Support", "", 3, "tools", "draft"),
    ]
    xml = render_agent_roster_xml(agents, ["a"])
    assert 'count="2"' in xml and 'focused="1"' in xml
    assert '<agent id="a" name="Sales" tables="12" status="published" focused="true">Revenue and orders</agent>' in xml
    # non-focused agent has no focused attr and empty body
    assert '<agent id="b" name="Support" tools="3" status="draft"></agent>' in xml


def test_seed_focus_prefers_usage_then_order():
    dss = [_ds(i, f"s{i}") for i in range(5)]
    usage = {"id2": 5.0, "id0": 2.0}
    seed = _seed_focus(dss, usage, 3)
    assert seed[0] == "id2" and seed[1] == "id0"  # usage-ranked first
    assert len(seed) == 3


@pytest.mark.asyncio
async def test_one_liner_uses_description_without_db():
    dss = [_ds(0, "A", desc="  Revenue   and orders  "), _ds(1, "B", desc=None)]
    # db is never touched when every agent either has a description or no primary
    out = await load_agent_one_liners(None, dss)
    assert out["id0"] == "Revenue and orders"  # whitespace collapsed
    assert out["id1"] == ""


@pytest.mark.asyncio
async def test_build_focus_all_when_few_agents_no_db():
    org = SimpleNamespace(id="org")
    dss = [_ds(i, f"s{i}", desc="x") for i in range(DEFAULT_INDEX_THRESHOLD)]  # == threshold, not over
    secs = [_section(d.id, tables=1) for d in dss]
    focus, roster, mode = await build_focus_and_roster(None, org, None, dss, secs, None)
    assert mode == "all" and focus is None and roster is None


@pytest.mark.asyncio
async def test_build_focus_explicit_selection_no_db():
    org = SimpleNamespace(id="org")
    dss = [_ds(i, f"s{i}", desc="x") for i in range(6)]  # over threshold
    secs = [_section(d.id, tables=2) for d in dss]
    focus, roster, mode = await build_focus_and_roster(None, org, None, dss, secs, ["id3", "bogus"])
    assert mode == "focus"
    assert focus == ["id3"]  # bogus id (not attached) dropped
    assert 'focused="1"' in roster and 'count="6"' in roster
    # focused agent marked, others listed
    assert '<agent id="id3"' in roster and 'focused="true"' in roster
