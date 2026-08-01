"""Mode-scoped candidate resolution for the agent focus tools.

Requirements pinned here:
  - TRAINING: search/set operate only on agents the user can MANAGE.
  - Chat/deep MANUAL selection: the user's agents come first; other accessible
    agents are proposable but flagged as not attached (attached_ids excludes
    them — expanding to one requires user approval).
  - Chat/deep AUTO (empty selection): the working set is every accessible
    agent, and everything counts as in-scope (nothing needs attach/approval).
"""
from types import SimpleNamespace

import pytest

import app.core.permission_resolver as pr
import app.ai.tools.implementations.agent_focus_common as afc
from app.ai.tools.implementations.agent_focus_common import (
    report_selection_is_auto,
    resolve_candidate_agents,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Returns a fixed row set for any query (stands in for the DataSource select)."""
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _Result(self._rows)


def _agent(i):
    return SimpleNamespace(id=f"id{i}", name=f"agent{i}", organization_id="org", is_public=True)


@pytest.mark.asyncio
async def test_manual_scope_is_selection_plus_proposable(monkeypatch):
    async def fake_accessible(db, org, user):
        return [_agent(1), _agent(2), _agent(3)]  # org-wide accessible
    monkeypatch.setattr(afc, "accessible_agents", fake_accessible)

    report = SimpleNamespace(data_sources=[_agent(1), _agent(2)])  # manual pick
    agents, scope, attached_ids = await resolve_candidate_agents(
        _FakeDB([]), SimpleNamespace(id="org"), SimpleNamespace(id="u"), report, "chat"
    )
    assert scope == "attached+accessible"
    assert [a.id for a in agents] == ["id1", "id2", "id3"]  # selection first, extras after
    assert attached_ids == {"id1", "id2"}                    # id3 would need approval
    assert not report_selection_is_auto(report)


@pytest.mark.asyncio
async def test_auto_scope_is_all_accessible_nothing_needs_attach(monkeypatch):
    async def fake_accessible(db, org, user):
        return [_agent(1), _agent(2), _agent(3)]
    monkeypatch.setattr(afc, "accessible_agents", fake_accessible)

    report = SimpleNamespace(data_sources=[])  # Auto
    agents, scope, attached_ids = await resolve_candidate_agents(
        _FakeDB([]), SimpleNamespace(id="org"), SimpleNamespace(id="u"), report, "chat"
    )
    assert scope == "accessible"
    assert [a.id for a in agents] == ["id1", "id2", "id3"]
    assert attached_ids == {"id1", "id2", "id3"}  # all in scope — no approval path
    assert report_selection_is_auto(report)


@pytest.mark.asyncio
async def test_training_scope_is_manageable_only(monkeypatch):
    async def fake_perm(db, user_id, org_id, permission):
        assert permission == "manage_instructions"
        return (False, ["id2"])
    monkeypatch.setattr(pr, "get_ds_ids_with_permission", fake_perm)

    report = SimpleNamespace(data_sources=[_agent(1), _agent(2), _agent(3)])
    agents, scope, attached_ids = await resolve_candidate_agents(
        _FakeDB([_agent(2)]), SimpleNamespace(id="org"), SimpleNamespace(id="u"), report, "training"
    )
    assert scope == "managed"
    assert [a.id for a in agents] == ["id2"]  # NOT all attached — only the managed one
    assert attached_ids == {"id1", "id2", "id3"}


@pytest.mark.asyncio
async def test_training_admin_sees_all(monkeypatch):
    async def fake_perm(db, user_id, org_id, permission):
        return (True, [])  # full admin over agents
    monkeypatch.setattr(pr, "get_ds_ids_with_permission", fake_perm)

    all_agents = [_agent(1), _agent(2), _agent(3)]
    agents, scope, attached_ids = await resolve_candidate_agents(
        _FakeDB(all_agents), SimpleNamespace(id="org"), SimpleNamespace(id="u"),
        SimpleNamespace(data_sources=[]), "training"
    )
    assert scope == "managed"
    assert [a.id for a in agents] == ["id1", "id2", "id3"]
