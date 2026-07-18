"""Mode-scoped candidate resolution for the agent focus tools.

Asserts the requirement: in TRAINING mode search/set operate only on agents the
user can MANAGE; in chat/deep they operate on the report's attached agents.
"""
from types import SimpleNamespace

import pytest

import app.core.permission_resolver as pr
from app.ai.tools.implementations.agent_focus_common import resolve_candidate_agents


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
    return SimpleNamespace(id=f"id{i}", name=f"agent{i}", organization_id="org")


@pytest.mark.asyncio
async def test_chat_scope_is_report_attached_agents():
    report = SimpleNamespace(data_sources=[_agent(1), _agent(2)])
    agents, scope = await resolve_candidate_agents(
        _FakeDB([]), SimpleNamespace(id="org"), SimpleNamespace(id="u"), report, "chat"
    )
    assert scope == "attached"
    assert [a.id for a in agents] == ["id1", "id2"]


@pytest.mark.asyncio
async def test_training_scope_is_manageable_only(monkeypatch):
    # Non-admin user who manages only agent 2.
    async def fake_perm(db, user_id, org_id, permission):
        assert permission == "manage_instructions"
        return (False, ["id2"])
    monkeypatch.setattr(pr, "get_ds_ids_with_permission", fake_perm)

    # The DataSource query is scoped to those ids; the fake db returns just agent 2.
    report = SimpleNamespace(data_sources=[_agent(1), _agent(2), _agent(3)])
    agents, scope = await resolve_candidate_agents(
        _FakeDB([_agent(2)]), SimpleNamespace(id="org"), SimpleNamespace(id="u"), report, "training"
    )
    assert scope == "managed"
    assert [a.id for a in agents] == ["id2"]  # NOT all attached — only the managed one


@pytest.mark.asyncio
async def test_training_admin_sees_all(monkeypatch):
    async def fake_perm(db, user_id, org_id, permission):
        return (True, [])  # full admin over agents
    monkeypatch.setattr(pr, "get_ds_ids_with_permission", fake_perm)

    all_agents = [_agent(1), _agent(2), _agent(3)]
    agents, scope = await resolve_candidate_agents(
        _FakeDB(all_agents), SimpleNamespace(id="org"), SimpleNamespace(id="u"),
        SimpleNamespace(data_sources=[]), "training"
    )
    assert scope == "managed"
    assert [a.id for a in agents] == ["id1", "id2", "id3"]
