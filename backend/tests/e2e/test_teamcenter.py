"""Real connection API/DB flow and agent tools for the Siemens Teamcenter connector.

Only Teamcenter is simulated (tools/teamcenter mock: web tier, Security Services and file server on a real
socket); the app is not patched:

    POST /api/connections | /api/data_sources (type teamcenter)
      -> test / test-params / my-credentials (a member's login checked with Teamcenter first)
      -> schema indexing (one table per business object type + relation tables)
      -> SSO: GET /api/connections/{id}/oauth/authorize -> TcSS login -> /api/connections/teamcenter/sso/callback
      -> agent: resolve_file_client -> construct_client -> TeamcenterClient.execute_query / read_file
"""
import asyncio
import json
import uuid
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tests.unit.test_teamcenter_client import PART_TYPES, tc_server, tcm  # noqa: F401

pytestmark = pytest.mark.e2e

_REJECTED = "connection.teamcenter_login_rejected"
_CALLBACK = "/api/connections/teamcenter/sso/callback"


def _cfg(tcm, **extra):
    return {"base_url": tcm["base_url"], "allow_http": True, "object_types": PART_TYPES, **extra}


def _sso_cfg(tcm, **extra):
    return _cfg(tcm, sso_login_url=tcm["login_url"], sso_app_id="BOW", **extra)


def _admin(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    return token, org, {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def _member(test_client, create_user, login_user, org, admin_headers, login):
    email = f"tc-{login}-{uuid.uuid4().hex[:6]}@example.com"
    r = test_client.post(f"/api/organizations/{org}/members",
                         json={"organization_id": org, "email": email, "role": "member"}, headers=admin_headers)
    assert r.status_code == 200, r.json()
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def _source(tcm, test_client, create_data_source, token, org, headers, auth_policy="user_required", config=None):
    ds = create_data_source(name=f"Teamcenter {uuid.uuid4().hex[:4]}", type="teamcenter",
                            config=config or _cfg(tcm), auth_policy=auth_policy, user_token=token, org_id=org,
                            credentials={"username": "infodba", "password": tcm["mock"].USERS["infodba"]["password"]})
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=headers).status_code == 200
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    return ds["id"], connections[0]["id"]


def _save(test_client, conn_id, headers, credentials):
    return test_client.post(f"/api/connections/{conn_id}/my-credentials",
                            json={"auth_mode": "userpass", "credentials": credentials}, headers=headers)


def _users_seen(tcm):
    return {user for user, _ in tcm["mock"].REQUEST_LOG if user}


def _file_ids(response):
    assert response.status_code == 200, response.json()
    body = response.json()
    assert not body.get("connect_required"), body
    return {f["id"] for f in body["files"]}


def test_connection_lifecycle_and_secrets(
    tcm, test_client, create_user, login_user, whoami, create_connection, get_connection,
    test_connection_connectivity,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    secret = tcm["mock"].USERS["infodba"]["password"]
    conn = create_connection(name="Teamcenter QA", type="teamcenter", config=_cfg(tcm),
                             credentials={"username": "infodba", "password": secret}, user_token=token, org_id=org)
    assert secret not in json.dumps(get_connection(connection_id=conn["id"], user_token=token, org_id=org))
    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True, probe

    tcm["mock"].REQUEST_LOG.clear()
    r = test_client.post("/api/connections/test-params", headers=headers, json={
        "name": "Bad", "type": "teamcenter", "config": _cfg(tcm),
        "credentials": {"username": "infodba", "password": "wrong-tc-pass-e2e-4b"}})
    assert r.status_code == 200 and r.json()["success"] is False, r.json()
    assert "wrong-tc-pass-e2e-4b" not in r.text
    assert tcm["mock"].REQUEST_LOG, "the login was sent to Teamcenter"


def test_the_catalog_lists_a_table_per_type_with_custom_properties(
    tcm, test_client, create_user, login_user, whoami, create_data_source, wait_for_connection_indexing,
    get_connection_tables,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    _, conn_id = _source(tcm, test_client, create_data_source, token, org, headers, auth_policy="system_only")
    run = wait_for_connection_indexing(connection_id=conn_id, user_token=token, org_id=org)
    assert run is None or run["status"] == "completed", run
    listed = get_connection_tables(connection_id=conn_id, user_token=token, org_id=org)
    schema = listed.get("tables", listed) if isinstance(listed, dict) else listed
    names = {t["name"] for t in schema}
    assert {"a2_part_revision", "item_revision", "change_notice_revision", "bom", "where_used", "change_items",
            "revision_rules"} <= names, names
    parts = next(t for t in schema if t["name"] == "a2_part_revision")
    assert parts["column_count"] >= 18, parts      # standard columns + the site's a2_* properties


def test_a_login_teamcenter_rejects_is_refused_and_nothing_is_stored(
    tcm, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(tcm, test_client, create_data_source, token, org, headers)
    member = _member(test_client, create_user, login_user, org, headers, "jdoe")
    r = _save(test_client, conn_id, member, {"username": "jdoe", "password": "wrong-tc-save-8e"})
    assert r.status_code == 422 and r.json()["error_code"] == _REJECTED, r.text
    assert "wrong-tc-save-8e" not in r.text
    files = test_client.get(f"/api/data_sources/{ds_id}/connections/{conn_id}/files", headers=member)
    assert files.status_code == 200 and files.json().get("connect_required") is True, files.text


def test_members_read_teamcenter_as_themselves(
    tcm, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(tcm, test_client, create_data_source, token, org, headers)
    jdoe = _member(test_client, create_user, login_user, org, headers, "jdoe")
    asmith = _member(test_client, create_user, login_user, org, headers, "asmith")
    r = _save(test_client, conn_id, jdoe, {"username": "jdoe", "password": tcm["mock"].USERS["jdoe"]["password"]})
    assert r.status_code == 200 and r.json().get("identity") == "jdoe", r.json()
    assert _save(test_client, conn_id, asmith, {"username": "asmith",
                                                 "password": tcm["mock"].USERS["asmith"]["password"]}).status_code == 200
    endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    tcm["mock"].REQUEST_LOG.clear()
    _file_ids(test_client.get(endpoint, headers=jdoe))
    assert _users_seen(tcm) == {"jdoe"}
    tcm["mock"].REQUEST_LOG.clear()
    _file_ids(test_client.get(endpoint, headers=asmith))
    assert _users_seen(tcm) == {"asmith"}


def _tcss_sign_in(tcm, authorization_url, user):
    """The browser at Teamcenter Security Services: the user signs in, TcSS redirects to the app root URL."""
    app_id = dict(parse_qsl(urlsplit(authorization_url).query))["appID"]
    form = requests.post(tcm["root"] + "/tcssol/weblogin/login", allow_redirects=False,
                         data={"appID": app_id, "username": user, "password": tcm["mock"].USERS[user]["password"]})
    assert form.status_code == 302, form.text
    back = urlsplit(form.headers["Location"])
    assert back.path == _CALLBACK
    return f"{back.path}?{back.query}"


def test_tcss_sign_in_connects_a_member_who_then_reads_as_themselves(
    tcm, test_client, create_user, login_user, whoami, create_data_source,
):
    tcm["mock"].STATE["app_roots"]["BOW"] = "http://testserver" + _CALLBACK
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(tcm, test_client, create_data_source, token, org, headers, config=_sso_cfg(tcm))
    conn = test_client.get(f"/api/connections/{conn_id}", headers=headers).json()
    assert conn["allowed_user_auth_modes"] == ["oauth"], conn

    member = _member(test_client, create_user, login_user, org, headers, "jdoe")
    files = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    assert test_client.get(files, headers=member).json().get("connect_required") is True

    start = test_client.get(f"/api/connections/{conn_id}/oauth/authorize?return_to=/agents", headers=member)
    assert start.status_code == 200, start.text
    url = start.json()["authorization_url"]
    assert url.startswith(tcm["login_url"]) and "appID=BOW" in url
    callback = _tcss_sign_in(tcm, url, "jdoe")
    done = test_client.get(callback, follow_redirects=False)
    assert done.status_code in (302, 307) and "oauth=success" in done.headers["location"], done.headers

    tcm["mock"].REQUEST_LOG.clear()
    _file_ids(test_client.get(files, headers=member))
    assert _users_seen(tcm) == {"jdoe"}


def test_a_tcss_return_without_its_sign_in_is_turned_away(
    tcm, test_client, create_user, login_user, whoami, create_data_source,
):
    tcm["mock"].STATE["app_roots"]["BOW"] = "http://testserver" + _CALLBACK
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(tcm, test_client, create_data_source, token, org, headers, config=_sso_cfg(tcm))
    member = _member(test_client, create_user, login_user, org, headers, "jdoe")
    files = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"

    # A token arriving with no sign-in started from this browser is not stored.
    test_client.cookies.clear()
    stray = test_client.get(_tcss_sign_in(tcm, "x?appID=BOW", "jdoe"), follow_redirects=False)
    assert "oauth=error" in stray.headers["location"]
    # A token Teamcenter does not accept is rolled back.
    assert test_client.get(f"/api/connections/{conn_id}/oauth/authorize", headers=member).status_code == 200
    bad = test_client.get(_CALLBACK + "?TCSS_TOKEN=forged-token&TCSS_USERID=jdoe", follow_redirects=False)
    assert "oauth=error" in bad.headers["location"] and "forged-token" not in bad.headers["location"]
    # A return without any token names the parameters it got.
    assert test_client.get(f"/api/connections/{conn_id}/oauth/authorize", headers=member).status_code == 200
    empty = test_client.get(_CALLBACK + "?status=ok", follow_redirects=False)
    assert "oauth=error" in empty.headers["location"] and "status" in empty.headers["location"]
    assert test_client.get(files, headers=member).json().get("connect_required") is True


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


def test_agent_queries_tables_and_reads_an_attached_document(
    tcm, test_client, create_user, login_user, whoami, create_data_source, create_report,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _source(tcm, test_client, create_data_source, token, org, headers, auth_policy="system_only")
    report = create_report(title="Teamcenter agent", user_token=token, org_id=org, data_sources=[ds_id])

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
            bom = client.execute_query(json.dumps({"table": "bom", "root": "PRT-1000/B", "depth": 2}))
            search = [e async for e in SearchFilesTool().run_stream({"connection_id": conn_id, "query": "pump spec"}, ctx)]
            hits = search[-1].payload["output"]
            read = [e async for e in ReadFileTool().run_stream(
                {"connection_id": conn_id, "file_id": hits["files"][0]["id"]}, ctx)]
            return bom, hits, read[-1].payload["output"]

    bom, hits, read = asyncio.run(run())
    assert {"PRT-1001", "PRT-1002", "PRT-1004", "PRT-1005"} <= set(bom["item_id"])
    assert [f["id"] for f in hits["files"]] == ["dataset:DS_spec_pdf00001"]
    assert read["success"] is True and tcm["mock"].SPEC_CODE in read["text"], read
