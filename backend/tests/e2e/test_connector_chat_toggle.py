"""Per-user on/off switch for a personal connector in the chat composer.

Contract:
  * ``GET /data_sources/active`` reports ``chat_enabled`` for connectors
    (default True) and ``None`` for agents — the toggle does not apply to them.
  * ``PUT /data_sources/{id}/chat-preference`` remembers the choice per user +
    workspace; the active list reflects it on the next read, and it survives
    across requests (persistence). The toggle is rejected on an agent.
  * At run time the choice is enforced: a connector switched OFF is dropped from
    the run's working set (``resolve_run_agents``), so it contributes no client,
    context or tool definitions. An agent is never dropped, and one user's
    choice never affects another's.

The connector is seeded as a sqlite data source whose ``kind`` is then forced to
"connector" (and one connection to ``user_required``) directly in the DB: the
API path that produces a real personal connector needs a live MCP server, which
this behavioral test does not. Everything else runs through the real routes,
services and DB. Async DB access uses ``asyncio.run`` like the other e2e tests
(e.g. test_mcp_dynamic_params).

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_connector_chat_toggle.py -v
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

pytestmark = pytest.mark.e2e


def _bootstrap(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    return {"token": token, "org_id": org_id, "user_id": me["id"], "headers": headers}


def _sqlite_ds(create_data_source, token, org_id, *, name):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    return create_data_source(name=name, type="sqlite", config={"database": path},
                              credentials={}, user_token=token, org_id=org_id)


def _make_connector(data_source_id: str):
    """Force a data source to look like a personal connector in the DB."""
    async def _go():
        from app.dependencies import async_session_maker
        from app.models.data_source import DataSource
        async with async_session_maker() as s:
            ds = (await s.execute(
                select(DataSource).options(selectinload(DataSource.connections))
                .where(DataSource.id == str(data_source_id))
            )).scalars().first()
            ds.kind = "connector"
            for c in ds.connections:
                c.auth_policy = "user_required"
            await s.commit()
    asyncio.run(_go())


def _set_pref(user_id, org_id, data_source_id, enabled):
    async def _go():
        from app.dependencies import async_session_maker
        from app.services.connector_preference_service import set_connector_enabled
        async with async_session_maker() as s:
            await set_connector_enabled(s, str(user_id), str(org_id), str(data_source_id), enabled)
            await s.commit()
    asyncio.run(_go())


def _working_set(org_id, user_id, report_id):
    async def _go():
        from app.dependencies import async_session_maker
        from app.models.organization import Organization
        from app.models.user import User
        from app.models.report import Report
        from app.ai.tools.implementations.agent_focus_common import resolve_run_agents
        async with async_session_maker() as s:
            org = await s.get(Organization, str(org_id))
            user = await s.get(User, str(user_id))
            rep = (await s.execute(
                select(Report).options(selectinload(Report.data_sources))
                .where(Report.id == str(report_id))
            )).scalars().first()
            agents = await resolve_run_agents(s, org, user, rep)
            return {str(d.id) for d in agents}
    return asyncio.run(_go())


def _active(client, headers):
    r = client.get("/api/data_sources/active?include_unconnected=true", headers=headers)
    assert r.status_code == 200, r.text
    return {d["id"]: d for d in r.json()}


def test_chat_toggle_round_trips_through_the_active_list(
    test_client, create_user, login_user, whoami, create_data_source
):
    a = _bootstrap(create_user, login_user, whoami)
    connector = _sqlite_ds(create_data_source, a["token"], a["org_id"], name=f"GitHub {uuid.uuid4().hex[:5]}")
    agent = _sqlite_ds(create_data_source, a["token"], a["org_id"], name=f"Warehouse {uuid.uuid4().hex[:5]}")
    _make_connector(connector["id"])

    # Default is ON for a connector; agents carry no toggle state.
    rows = _active(test_client, a["headers"])
    assert rows[connector["id"]]["chat_enabled"] is True
    assert rows[agent["id"]]["chat_enabled"] is None

    # Turn it off; the active list reflects it on the next read.
    r = test_client.put(f"/api/data_sources/{connector['id']}/chat-preference",
                        headers=a["headers"], json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert _active(test_client, a["headers"])[connector["id"]]["chat_enabled"] is False

    # Turn it back on.
    r = test_client.put(f"/api/data_sources/{connector['id']}/chat-preference",
                        headers=a["headers"], json={"enabled": True})
    assert r.status_code == 200, r.text
    assert _active(test_client, a["headers"])[connector["id"]]["chat_enabled"] is True

    # The toggle only applies to connectors.
    r = test_client.put(f"/api/data_sources/{agent['id']}/chat-preference",
                        headers=a["headers"], json={"enabled": False})
    assert r.status_code == 400, r.text


def test_disabled_connector_is_dropped_from_the_run_working_set(
    test_client, create_user, login_user, whoami, create_data_source, create_report
):
    a = _bootstrap(create_user, login_user, whoami)
    connector = _sqlite_ds(create_data_source, a["token"], a["org_id"], name=f"GitHub {uuid.uuid4().hex[:5]}")
    agent = _sqlite_ds(create_data_source, a["token"], a["org_id"], name=f"Warehouse {uuid.uuid4().hex[:5]}")
    _make_connector(connector["id"])
    # An unattached report is Auto: its working set is the user's accessible
    # agents, connector included.
    report = create_report(title="auto", user_token=a["token"], org_id=a["org_id"], data_sources=[])

    before = _working_set(a["org_id"], a["user_id"], report["id"])
    assert connector["id"] in before
    assert agent["id"] in before

    _set_pref(a["user_id"], a["org_id"], connector["id"], False)
    after = _working_set(a["org_id"], a["user_id"], report["id"])
    assert connector["id"] not in after, "a disabled connector must not reach the run"
    assert agent["id"] in after, "an agent must never be dropped by the connector toggle"

    _set_pref(a["user_id"], a["org_id"], connector["id"], True)
    assert connector["id"] in _working_set(a["org_id"], a["user_id"], report["id"])


def test_one_users_choice_does_not_affect_another(
    test_client, create_user, login_user, whoami, create_data_source, create_report
):
    a = _bootstrap(create_user, login_user, whoami)
    connector = _sqlite_ds(create_data_source, a["token"], a["org_id"], name=f"GitHub {uuid.uuid4().hex[:5]}")
    _make_connector(connector["id"])
    test_client.put(f"/api/data_sources/{connector['id']}", headers=a["headers"], json={"is_public": True})

    # A second user in the same org (invite, then register the same email).
    b_email = f"peer_{uuid.uuid4().hex[:6]}@test.com"
    r = test_client.post(f"/api/organizations/{a['org_id']}/members", headers=a["headers"],
                        json={"organization_id": a["org_id"], "email": b_email, "role": "member"})
    assert r.status_code in (200, 201), r.text
    create_user(email=b_email, password="test123")
    b_token = login_user(b_email, "test123")
    b_id = whoami(b_token)["id"]

    report = create_report(title="auto", user_token=a["token"], org_id=a["org_id"], data_sources=[])

    # A disables the connector for themselves only.
    _set_pref(a["user_id"], a["org_id"], connector["id"], False)

    assert connector["id"] not in _working_set(a["org_id"], a["user_id"], report["id"])
    assert connector["id"] in _working_set(a["org_id"], b_id, report["id"]), \
        "B never turned it off, so B still gets it"
