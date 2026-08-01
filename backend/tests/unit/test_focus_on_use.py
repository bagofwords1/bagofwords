"""Focus-on-use — focus is a side-effect of use, never a model obligation.

Contracts (PO-scan review follow-up):
- Successful file-tool use focuses the agent(s) the resolvers marked as used.
- An explicit focus (user / set_report_agents) is never overridden.
- A focus set BY THIS MECHANISM may grow as more agents get used in the same
  run (multi-source scans: OneDrive then Drive).
- Failed calls and unknown agents never change focus.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.ai.agent_v2 import AgentV2


class _FakeDB:
    def __init__(self):
        self.committed = 0

    def add(self, obj):
        pass

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        pass


def _agent(attached_ids, explicit_focus=None, used=(), set_by_use=False):
    a = AgentV2.__new__(AgentV2)
    a.report = SimpleNamespace(focused_data_source_ids=list(explicit_focus or []))
    a.db = _FakeDB()
    a.data_sources = [SimpleNamespace(id=i) for i in attached_ids]
    a.used_agent_ids = set(used)
    a.loaded_agent_ids = set()
    a._focus_set_by_use = set_by_use
    return a


def _run(coro):
    return asyncio.run(coro)


def test_file_tool_use_sets_focus():
    a = _agent(["od", "gd", "ms"], used=["od"])
    _run(a._persist_focus_on_use("search_files", {"connection_id": "od"}, {"success": True}))
    assert a.report.focused_data_source_ids == ["od"]
    assert a._focus_set_by_use is True


def test_use_grows_focus_set_by_use_only():
    a = _agent(["od", "gd"], explicit_focus=["od"], used=["od", "gd"], set_by_use=True)
    _run(a._persist_focus_on_use("list_files", {}, {"success": True}))
    assert a.report.focused_data_source_ids == ["od", "gd"]


def test_explicit_focus_is_never_overridden():
    a = _agent(["od", "gd"], explicit_focus=["gd"], used=["od"], set_by_use=False)
    _run(a._persist_focus_on_use("read_file", {}, {"success": True}))
    assert a.report.focused_data_source_ids == ["gd"]
    assert a.db.committed == 0


def test_failures_and_unknown_agents_do_not_focus():
    a = _agent(["od"], used=["od"])
    _run(a._persist_focus_on_use("search_files", {}, {"success": False}))
    assert a.report.focused_data_source_ids == []
    b = _agent(["od"], used=["not-attached"])
    _run(b._persist_focus_on_use("search_files", {}, {"success": True}))
    assert b.report.focused_data_source_ids == []


def test_non_use_tools_do_not_focus():
    a = _agent(["od"], used=["od"])
    _run(a._persist_focus_on_use("describe_tables", {}, {"success": True}))
    assert a.report.focused_data_source_ids == []


def test_data_tool_targeting_still_wins_over_marker():
    # tables_by_source names the agent explicitly — that beats the run marker
    a = _agent(["od", "ms"], used=["od"])
    _run(a._persist_focus_on_use(
        "create_data",
        {"tables_by_source": [{"data_source_id": "ms"}]},
        {"success": True},
    ))
    assert a.report.focused_data_source_ids == ["ms"]
