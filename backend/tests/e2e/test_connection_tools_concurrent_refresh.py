"""Tool discovery for one connection can run twice at once, and must converge.

Creating an agent on a tool connection (MCP / custom API) starts a background
indexing run AND discovers the tools in the request; an admin's "refresh tools"
can also overlap a scheduled reindex. Each run read the stored tools and then
inserted the missing ones, so two runs that both saw a tool as new both
inserted it: the loser hit uq_connection_tool_name, and in agent creation that
poisoned the session and failed the whole create with a 500.

The provider client is the mocked boundary; it holds both runs until each has
asked for the tool list, which makes the overlap deterministic instead of a
matter of CI timing.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_connection_tools_concurrent_refresh.py -v
"""
import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_tool import ConnectionTool
from app.services.connection_service import ConnectionService
from tests.mocks.mock_mcp_server import MockToolProviderClient


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _tools(names):
    return [{"name": n, "description": n, "input_schema": {"type": "object"}, "output_schema": {}} for n in names]


class _BothReadFirst(MockToolProviderClient):
    """Returns the tool list only once `parties` callers are all waiting for it,
    so every run reads the stored tools before any of them writes."""

    def __init__(self, tools, parties):
        super().__init__(tools=tools)
        self._parties = parties
        self._arrived = 0
        self._all_here = asyncio.Event()

    async def alist_tools(self):
        self._arrived += 1
        if self._arrived >= self._parties:
            self._all_here.set()
        await asyncio.wait_for(self._all_here.wait(), timeout=10)
        return self._tools


async def _refresh_concurrently(connection_id, new_names, runs):
    client = _BothReadFirst(_tools(new_names), parties=runs)

    async def _mock_construct(self, db, connection, current_user=None, **kwargs):
        return client

    async def _one():
        async with async_session_maker() as db:
            conn = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
            return await ConnectionService().refresh_tools(db=db, connection=conn)

    with patch.object(ConnectionService, "construct_client", _mock_construct):
        return await asyncio.gather(*(_one() for _ in range(runs)), return_exceptions=True)


async def _stored_names(connection_id):
    async with async_session_maker() as db:
        rows = (await db.execute(select(ConnectionTool.name).where(
            ConnectionTool.connection_id == connection_id))).all()
    return sorted(r[0] for r in rows)


@pytest.mark.e2e
@pytest.mark.parametrize("runs,new_names", [
    (2, ["get_orders", "get_customers", "get_invoices"]),
    (3, ["list_items"]),
])
def test_overlapping_tool_refreshes_all_succeed_and_store_each_tool_once(
    runs, new_names, create_user, login_user, whoami, create_custom_api_connection,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    conn = create_custom_api_connection(name=f"api-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    before = asyncio.run(_stored_names(conn["id"]))
    assert before and not set(before) & set(new_names)  # every run must insert AND delete

    results = asyncio.run(_refresh_concurrently(conn["id"], new_names, runs))

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    assert asyncio.run(_stored_names(conn["id"])) == sorted(new_names)
    for r in results:
        assert sorted(t.name for t in r) == sorted(new_names)


@pytest.mark.e2e
def test_agent_creation_survives_a_failed_tool_discovery(
    test_client, create_user, login_user, whoami, create_custom_api_connection,
):
    """Tool discovery on agent create is best-effort: if it fails mid-flush the
    agent must still be created, not 500 on a session left needing rollback."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    conn = create_custom_api_connection(name=f"api-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id)
    stored = asyncio.run(_stored_names(conn["id"]))

    async def _failing_refresh(self, db, connection, current_user=None):
        # A flush that violates uq_connection_tool_name, like the losing run did.
        db.add(ConnectionTool(name=stored[0], connection_id=str(connection.id)))
        await db.flush()

    name = f"agent-{uuid.uuid4().hex[:6]}"
    with patch.object(ConnectionService, "refresh_tools", _failing_refresh):
        r = test_client.post("/api/data_sources", json={"name": name, "connection_ids": [conn["id"]]},
                             headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == name
    agents = test_client.get("/api/data_sources", headers=_h(token, org_id)).json()
    assert name in {a["name"] for a in agents}
    assert asyncio.run(_stored_names(conn["id"])) == stored
