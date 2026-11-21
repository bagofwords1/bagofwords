"""Real connection API/DB flow and agent tools for the Jira Data Center connector.

Only the Jira REST API is simulated (tools/jira mock, an in-process server on a
real socket); the app is not patched:

    POST /api/connections | /api/data_sources (type jira_dc)
      -> test / my-credentials (member login checked with Jira first)
      -> schema indexing (fixed table list + the site's custom fields)
      -> GET .../connections/{id}/files (issues as documents, live, as the resolved identity)
      -> agent: resolve_file_client -> construct_client -> JiraDcClient.execute_query / read_file
"""
import asyncio
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tests.unit.test_jira_dc_client import jira, jira_server  # noqa: F401

pytestmark = pytest.mark.e2e

_REJECTED = "connection.jira_login_rejected"
_CAPTCHA = "connection.jira_login_captcha"


def _token(jira, user):
    return next(t for t, u in jira["mock"].TOKENS.items() if u == user)


def _cfg(jira, auth_type="token", **extra):
    return {"base_url": jira["base_url"], "allow_http": True, "auth_type": auth_type, **extra}


def _admin(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    return token, org, {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def _member(test_client, create_user, login_user, org, admin_headers, login):
    email = f"jira-{login}-{uuid.uuid4().hex[:6]}@example.com"
    r = test_client.post(f"/api/organizations/{org}/members",
                         json={"organization_id": org, "email": email, "role": "member"}, headers=admin_headers)
    assert r.status_code == 200, r.json()
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def _source(jira, test_client, create_data_source, token, org, headers, auth_policy="user_required", **cfg):
    ds = create_data_source(name=f"Jira {uuid.uuid4().hex[:4]}", type="jira_dc", config=_cfg(jira, **cfg),
                            credentials={"personal_access_token": _token(jira, "svc")},
                            auth_policy=auth_policy, user_token=token, org_id=org)
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=headers).status_code == 200
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    return ds["id"], connections[0]["id"]


def _file_ids(response):
    assert response.status_code == 200, response.json()
    body = response.json()
    assert not body.get("connect_required"), body
    return {f["id"] for f in body["files"]}


def _users_seen(jira):
    return {user for user, _, _ in jira["mock"].REQUEST_LOG}


def _save(test_client, conn_id, headers, auth_mode, credentials):
    return test_client.post(f"/api/connections/{conn_id}/my-credentials",
                            json={"auth_mode": auth_mode, "credentials": credentials}, headers=headers)


def test_connection_lifecycle_and_secrets(
    jira, test_client, create_user, login_user, whoami, create_connection, get_connection,
    test_connection_connectivity,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    secret = _token(jira, "svc")
    conn = create_connection(name="Jira QA", type="jira_dc", config=_cfg(jira),
                             credentials={"personal_access_token": secret}, user_token=token, org_id=org)
    assert secret not in str(get_connection(connection_id=conn["id"], user_token=token, org_id=org))
    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True, probe

    jira["mock"].REQUEST_LOG.clear()
    r = test_client.post("/api/connections/test-params", headers=headers, json={
        "name": "Bad", "type": "jira_dc", "config": _cfg(jira, auth_type="userpass"),
        "credentials": {"username": "svc", "password": "wrong-jira-pass-e2e-9a"}})
    assert r.status_code == 200 and r.json()["success"] is False, r.json()
    assert "wrong-jira-pass-e2e-9a" not in r.text
    assert jira["mock"].REQUEST_LOG, "the login was sent to Jira"


def test_the_catalog_lists_the_jira_tables_with_custom_fields(
    jira, test_client, create_user, login_user, whoami, create_data_source, wait_for_connection_indexing,
    get_connection_tables,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    _, conn_id = _source(jira, test_client, create_data_source, token, org, headers, auth_policy="system_only",
                         projects="OPS")
    run = wait_for_connection_indexing(connection_id=conn_id, user_token=token, org_id=org)
    assert run is None or run["status"] == "completed", run
    listed = get_connection_tables(connection_id=conn_id, user_token=token, org_id=org)
    schema = listed.get("tables", listed) if isinstance(listed, dict) else listed
    names = {t["name"] for t in schema}
    assert {"issues", "issue_changelog", "issue_comments", "worklogs", "sprints", "versions"} <= names, names
    # The site's custom fields (here: Story Points) are indexed as extra issues columns.
    from app.data_sources.clients.jira_dc_client import TABLE_COLUMNS
    issues = next(t for t in schema if t["name"] == "issues")
    assert issues["column_count"] == len(TABLE_COLUMNS["issues"]) + 1, issues


@pytest.mark.parametrize("auth_mode,credentials,secret", [
    ("userpass", {"username": "bob", "password": "wrong-jira-pass-save-5d1"}, "wrong-jira-pass-save-5d1"),
    ("token", {"personal_access_token": "NotAJiraPat8c2e"}, "NotAJiraPat8c2e"),
], ids=["userpass", "token"])
def test_a_login_jira_rejects_is_refused_and_nothing_is_stored(
    jira, test_client, create_user, login_user, whoami, create_data_source, auth_mode, credentials, secret,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(jira, test_client, create_data_source, token, org, headers)
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    r = _save(test_client, conn_id, bob_h, auth_mode, credentials)
    assert r.status_code == 422 and r.json()["error_code"] == _REJECTED, r.text
    assert secret not in r.text
    files = test_client.get(f"/api/data_sources/{ds_id}/connections/{conn_id}/files", headers=bob_h)
    assert files.status_code == 200 and files.json().get("connect_required") is True, files.text


def test_a_captcha_locked_account_is_refused_with_its_own_code(
    jira, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    _, conn_id = _source(jira, test_client, create_data_source, token, org, headers)
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    jira["mock"].STATE["captcha_users"] = {"bob"}
    r = _save(test_client, conn_id, bob_h, "userpass", {"username": "bob", "password": jira["mock"].USERS["bob"]["password"]})
    assert r.status_code == 422 and r.json()["error_code"] == _CAPTCHA, r.text
    assert _save(test_client, conn_id, bob_h, "token", {"personal_access_token": _token(jira, "bob")}).status_code == 200


def test_members_read_issues_as_themselves(
    jira, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(jira, test_client, create_data_source, token, org, headers, projects="OPS")
    endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    alice_h = _member(test_client, create_user, login_user, org, headers, "alice")

    r = _save(test_client, conn_id, bob_h, "userpass", {"username": "bob", "password": jira["mock"].USERS["bob"]["password"]})
    assert r.status_code == 200 and r.json().get("identity") == "bob", r.json()
    assert _save(test_client, conn_id, alice_h, "token", {"personal_access_token": _token(jira, "alice")}).status_code == 200

    jira["mock"].REQUEST_LOG.clear()
    bob_files = _file_ids(test_client.get(endpoint, headers=bob_h))
    assert _users_seen(jira) == {"bob"}
    jira["mock"].REQUEST_LOG.clear()
    alice_files = _file_ids(test_client.get(endpoint, headers=alice_h))
    assert _users_seen(jira) == {"alice"}
    assert "issue:OPS-7" in alice_files and "issue:OPS-7" not in bob_files      # issue security
    assert all(f.startswith("issue:OPS-") for f in alice_files | bob_files)      # project scope


async def _loaded(db, ds_id, report_id):
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                           .where(DataSource.id == ds_id))).scalar_one()
    org = await db.get(Organization, ds.organization_id)
    report = (await db.execute(select(Report).where(Report.id == report_id)
                               .options(selectinload(Report.data_sources).selectinload(DataSource.connections)))).scalar_one()
    return ds, org, await db.get(User, report.user_id), report


def test_agent_queries_tables_and_reads_an_issue_inside_the_scope(
    jira, test_client, create_user, login_user, whoami, create_data_source, create_report,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(jira, test_client, create_data_source, token, org, headers, auth_policy="system_only",
                             projects="OPS")
    report = create_report(title="Jira agent", user_token=token, org_id=org, data_sources=[ds_id])

    async def run():
        from app.ai.tools.implementations._file_tool_common import resolve_file_client
        from app.ai.tools.implementations.read_file import ReadFileTool
        from app.ai.tools.implementations.search_files import SearchFilesTool
        from app.data_sources.clients.base import Capability
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            ds, org_obj, user, rep = await _loaded(db, ds_id, report["id"])
            ctx = {"db": db, "organization": org_obj, "user": user, "report": rep}
            client, err = await resolve_file_client(ctx, conn_id, Capability.READ_FILE)
            assert err is None, err
            open_bugs = client.execute_query(json.dumps({"table": "issues", "jql": "status != Done",
                                                         "fields": ["key", "project", "status", "assignee"]}))
            history = client.execute_query({"table": "issue_changelog", "jql": "resolved >= -40d"})
            read = [e async for e in ReadFileTool().run_stream({"connection_id": conn_id, "file_id": "issue:OPS-6"}, ctx)]
            outside = [e async for e in ReadFileTool().run_stream({"connection_id": conn_id, "file_id": "SEC-1"}, ctx)]
            search = [e async for e in SearchFilesTool().run_stream({"connection_id": conn_id, "query": "failover"}, ctx)]
            return open_bugs, history, read[-1].payload["output"], outside[-1].payload["output"], search[-1].payload["output"]

    open_bugs, history, read, outside, search = asyncio.run(run())
    assert set(open_bugs["project"]) == {"OPS"} and len(open_bugs) == 5
    assert {"In Progress", "Done"} <= set(history[history["field"] == "status"]["to_value"])
    assert read["success"] is True and "relates to OPS-1" in read["text"], read
    assert outside["success"] is False, outside
    assert {f["id"] for f in search["files"]} == {"issue:OPS-1", "issue:OPS-2", "issue:OPS-6"}
