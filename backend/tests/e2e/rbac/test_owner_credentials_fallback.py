"""Who may fall back to a per-user connection's shared credentials.

On a ``user_required`` connection a user with no saved login of their own is
either an org-level admin (``full_admin`` or ``manage_connections``), who runs
with the connection's shared credentials, or someone who must connect first.
Owning the data source and holding a per-agent ``manage`` grant unlock nothing.

Invariants under test:
- A non-admin owner with no login reaches the shared login on no path: neither
  connection Test route, query runs, a report rerun, or a scheduled run.
- Every status the UI reads says the same thing as the resolver: a refused user
  sees ``effective_auth`` "none", because the frontend hides Connect when it
  says "system".
- An org admin who does not own the source, and a holder of
  ``manage_connections``, keep the fallback.
- Once the owner saves their own login, runs use that login.

PostgreSQL stands in for every user/password connector. Only its engine is
stubbed, so the whole client runs and each login that reaches the driver is
recorded. A non-admin cannot create a data source, so the owner builds it as an
admin and is then demoted.
"""
import asyncio
import uuid
from unittest.mock import patch

import pytest
import sqlalchemy
from sqlalchemy.engine import make_url
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from app.dependencies import async_session_maker
from tests.e2e.rbac.test_viewer_run_shared_artifacts import (
    _headers,
    _seed_artifact_graph,
    _set_step_code,
)

SHARED_LOGIN = "shared_service_account"
OWN_LOGIN = "owner_personal_login"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def logins(monkeypatch):
    """Every login that reaches the PostgreSQL driver, in order."""
    seen = []

    def engine_for(uri, **_):
        seen.append(make_url(uri).username)
        return sqlalchemy.create_engine("sqlite://", poolclass=StaticPool)

    monkeypatch.setattr("app.data_sources.clients.postgresql_client.get_engine", engine_for)
    return seen


@pytest.fixture
def world(logins, test_client, bootstrap_admin, invite_user_to_org, create_data_source, create_report):
    """A public per-user Postgres source, its now non-admin owner, and the owner's report."""
    admin = bootstrap_admin()
    org_id, admin_h = admin["org_id"], _headers(admin["token"], admin["org_id"])
    owner = invite_user_to_org(org_id=org_id, admin_token=admin["token"], role="admin")
    owner_h = _headers(owner["token"], org_id)
    ds_name = f"Warehouse {uuid.uuid4().hex[:8]}"
    ds = create_data_source(
        name=ds_name, type="postgresql",
        config={"host": "localhost", "port": 5432, "database": "warehouse"},
        credentials={"user": SHARED_LOGIN, "password": "not-a-real-password"},
        auth_policy="user_required", user_token=owner["token"], org_id=org_id,
    )
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=owner_h).status_code == 200
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=owner_h).json()

    demoted = test_client.put(f"/api/organizations/{org_id}/members/{owner['membership_id']}",
                              json={"role": "member"}, headers=admin_h)
    assert demoted.status_code == 200 and demoted.json()["role"] == "member", demoted.json()

    report = create_report(title="Owner report", user_token=owner["token"], org_id=org_id, data_sources=[ds["id"]])
    seeded = _run(_seed_artifact_graph(report["id"]))
    _run(_set_step_code(seeded["step_ids"][0], _query_code(ds_name)))

    logins.clear()
    return {
        "admin": admin, "owner": owner, "owner_h": owner_h, "org_id": org_id, "ds_id": ds["id"], "ds_name": ds_name,
        "conn_id": connections[0]["id"], "report_id": report["id"], "query_id": seeded["query_ids"][0],
    }


def _query_code(ds_name: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients[{ds_name!r}].execute_query('SELECT 1 AS ok')\n"
    )


def _test_routes(test_client, world, headers):
    """Both connection Test routes. They load the connection fresh, unlike a run."""
    return [
        test_client.post(f"/api/connections/{world['conn_id']}/test-my-credentials", headers=headers),
        test_client.get(f"/api/data_sources/{world['ds_id']}/test_connection", headers=headers),
    ]


def _statuses(test_client, world, headers):
    """The status of the connection on every route the UI reads it from."""
    source = test_client.get(f"/api/data_sources/{world['ds_id']}", headers=headers)
    assert source.status_code == 200, source.text
    listed = test_client.get("/api/connections", headers=headers)
    assert listed.status_code == 200, listed.text
    connection = next(c for c in listed.json() if c["id"] == world["conn_id"])
    return {
        "data source": source.json()["user_status"],
        "data source connection": source.json()["connections"][0]["user_status"],
        "connection list": connection["user_status"],
    }


def _scheduled_rerun(world):
    from app.services.report_service import ReportService

    with patch("app.services.report_service.claim_scheduled_run", return_value=True):
        _run(ReportService().scheduled_rerun_report_steps(world["report_id"], world["owner"]["user_id"], world["org_id"]))


async def _agent_run_clients(world):
    """The client set a scheduled task's agent gets, loaded as a run loads the report."""
    from app.ai.tools.implementations.agent_focus_common import prepare_run_agents
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async with async_session_maker() as db:
        report = (await db.execute(
            sqlalchemy.select(Report)
            .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
            .where(Report.id == world["report_id"])
        )).scalar_one()
        user = await db.get(User, world["owner"]["user_id"])
        organization = await db.get(Organization, world["org_id"])
        _, clients = await prepare_run_agents(db, organization, user, report)
        return clients


@pytest.mark.e2e
def test_non_admin_owner_cannot_test_a_connection_with_the_shared_login(world, logins, test_client):
    my_credentials, data_source = _test_routes(test_client, world, world["owner_h"])

    assert my_credentials.status_code == 200, my_credentials.text
    assert my_credentials.json()["success"] is False, my_credentials.json()
    assert data_source.status_code == 200, data_source.text
    assert data_source.json()["success"] is False, data_source.json()
    assert logins == [], "the owner reached the shared login"


@pytest.mark.e2e
def test_status_tells_a_non_admin_owner_to_connect(world, test_client):
    for route, status in _statuses(test_client, world, world["owner_h"]).items():
        assert status["effective_auth"] == "none", (route, status)
        assert status["uses_fallback"] is False, (route, status)
        assert status["has_user_credentials"] is False, (route, status)


@pytest.mark.e2e
def test_the_direct_data_source_status_refuses_a_non_admin_owner_too(world):
    """build_user_status has no route today; it must not keep the old rule for a later caller."""
    from app.models.data_source import DataSource
    from app.models.user import User
    from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService

    async def status():
        async with async_session_maker() as db:
            ds = (await db.execute(
                sqlalchemy.select(DataSource).options(selectinload(DataSource.connections))
                .where(DataSource.id == world["ds_id"])
            )).scalar_one()
            user = await db.get(User, world["owner"]["user_id"])
            return await UserDataSourceCredentialsService().build_user_status(db, ds, user)

    built = _run(status())
    assert built.effective_auth == "none", built
    assert built.uses_fallback is False, built


@pytest.mark.e2e
def test_non_admin_owner_runs_are_refused(world, logins, test_client):
    """Interactive runs, a report rerun, and both kinds of schedule run as the owner."""
    viewer_run = test_client.post(f"/api/queries/{world['query_id']}/run", json={"mode": "viewer"},
                                  headers=world["owner_h"])
    assert 400 <= viewer_run.status_code < 500, viewer_run.text

    rerun = test_client.post(f"/api/reports/{world['report_id']}/rerun", headers=world["owner_h"])
    assert rerun.status_code == 200, rerun.text
    assert (rerun.json()["steps_succeeded"], rerun.json()["steps_failed"]) == (0, 1), rerun.json()

    _scheduled_rerun(world)
    assert _run(_agent_run_clients(world)) == {}, "the agent must get no client for the source"

    assert logins == [], "a run reached the shared login"


@pytest.mark.e2e
def test_org_admin_who_does_not_own_the_source_keeps_the_fallback(world, logins, test_client):
    headers = _headers(world["admin"]["token"], world["org_id"])

    for route, status in _statuses(test_client, world, headers).items():
        assert status["effective_auth"] == "system", (route, status)
        assert status["uses_fallback"] is True, (route, status)

    for response in _test_routes(test_client, world, headers):
        assert response.json()["success"] is True, response.json()
    assert set(logins) == {SHARED_LOGIN}


@pytest.mark.e2e
def test_manage_connections_holder_keeps_the_fallback(
    world, logins, test_client, invite_user_to_org, create_role, assign_role, grant_resource,
):
    token, org_id = world["admin"]["token"], world["org_id"]
    holder = invite_user_to_org(org_id=org_id, admin_token=token)
    granted = grant_resource(resource_type="data_source", resource_id=world["ds_id"], principal_type="user",
                             principal_id=holder["user_id"], permissions=["view"], user_token=token, org_id=org_id)
    assert granted.status_code == 200, granted.text
    role = create_role(name=f"connections-{uuid.uuid4().hex[:6]}", permissions=["manage_connections"],
                       user_token=token, org_id=org_id)
    assert role.status_code == 200, role.text
    assigned = assign_role(role_id=role.json()["id"], principal_type="user", principal_id=holder["user_id"],
                           user_token=token, org_id=org_id)
    assert assigned.status_code == 200, assigned.text
    headers = _headers(holder["token"], org_id)

    for route, status in _statuses(test_client, world, headers).items():
        assert status["effective_auth"] == "system", (route, status)

    my_credentials = test_client.post(f"/api/connections/{world['conn_id']}/test-my-credentials", headers=headers)
    assert my_credentials.json()["success"] is True, my_credentials.json()
    assert set(logins) == {SHARED_LOGIN}


@pytest.mark.e2e
def test_per_agent_manage_grant_does_not_unlock_the_shared_login(world, logins, test_client, invite_user_to_org, grant_resource):
    token, org_id = world["admin"]["token"], world["org_id"]
    manager = invite_user_to_org(org_id=org_id, admin_token=token)
    granted = grant_resource(resource_type="data_source", resource_id=world["ds_id"], principal_type="user",
                             principal_id=manager["user_id"], permissions=["view", "manage"],
                             user_token=token, org_id=org_id)
    assert granted.status_code == 200, granted.text
    headers = _headers(manager["token"], org_id)

    for route, status in _statuses(test_client, world, headers).items():
        assert status["effective_auth"] == "none", (route, status)

    for response in _test_routes(test_client, world, headers):
        assert response.json()["success"] is False, response.json()
    assert logins == []


@pytest.mark.e2e
def test_owner_who_saves_their_own_login_runs_as_themselves(world, logins, test_client):
    saved = test_client.post(f"/api/data_sources/{world['ds_id']}/my-credentials",
                             json={"auth_mode": "userpass", "credentials": {"user": OWN_LOGIN, "password": "own-password"}},
                             headers=world["owner_h"])
    assert saved.status_code == 200, saved.text

    statuses = _statuses(test_client, world, world["owner_h"])
    for route in ("data source", "data source connection"):
        assert statuses[route]["effective_auth"] == "user", (route, statuses[route])
        assert statuses[route]["has_user_credentials"] is True, (route, statuses[route])
    # The connection list has no data source in scope, so it cannot see this login: never "system".
    assert statuses["connection list"]["effective_auth"] != "system", statuses["connection list"]

    # Both Test routes read connection-level logins only, which this save does not write. Whatever
    # they answer, they must not log in as the shared account (asserted below).
    _test_routes(test_client, world, world["owner_h"])

    rerun = test_client.post(f"/api/reports/{world['report_id']}/rerun", headers=world["owner_h"])
    assert rerun.status_code == 200, rerun.text
    assert (rerun.json()["steps_succeeded"], rerun.json()["steps_failed"]) == (1, 0), rerun.json()

    _scheduled_rerun(world)
    assert world["ds_name"] in _run(_agent_run_clients(world))

    assert logins and set(logins) == {OWN_LOGIN}, "every run must use the owner's own login"
