"""End to end: what the agent is told about a KMS Lighthouse connection, and what its file tools do with it.

    POST /api/data_sources (type kms_lighthouse) + POST /api/reports (data_sources=[ds])
      -> SchemaContextBuilder.build().render()       (what the planner is handed)
      -> list_files / search_files / read_file run_stream
         -> resolve_file_client -> construct_client -> KmsLighthouseClient -> mock KMS (real socket)

Only the KMS Lighthouse API is simulated (tools/kms_lighthouse mock, an in-process
server); the app is not patched.
"""
import asyncio
import re
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.data_sources.clients import kms_lighthouse_client as kc
from tests.unit.test_kms_lighthouse_client import kms, kms_server  # noqa: F401

pytestmark = pytest.mark.e2e

_ROOT = 10            # "Procedures" in the mock tree
_MAX_RESULTS = 25     # the configured listing cap the note must state

# Everything svc sees under Procedures (online only), by id and path. Offline 306 and the offline
# "Old forms" folder are hidden; the two folders titled "Forms" get their ids in their path segments.
_ROOT_ENTRIES = {
    "item:301": "Billing dispute.txt",
    "item:305": "Policy v2.1 - summary.txt",
    "item:302": "Billing/Refund a payment.txt",
    "scenario:4401": "Billing/Card blocked flow.txt",
    "item:303": "Forms [13]/Change of address form.txt",
    "item:304": "Forms [14]/Card replacement form.txt",
}
_BILLING_ENTRIES = {"item:302": "Billing/Refund a payment.txt", "scenario:4401": "Billing/Card blocked flow.txt"}


def _setup(kms, test_client, create_user, login_user, whoami, create_data_source, create_report):
    """An admin's system-only KMS data source (svc credentials, rooted at Procedures) and a report using it."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    ds = create_data_source(
        name="KMS procedures", type="kms_lighthouse",
        config={"base_url": kms["base_url"], "allow_http": True, "index_mode": "none", "auth_type": "userpass",
                "root_folder_id": _ROOT, "max_results": _MAX_RESULTS},
        credentials={"username": "svc", "password": kms["mock"].USERS["svc"]["password"]},
        auth_policy="system_only", user_token=token, org_id=org,
    )
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    assert [c["type"] for c in connections] == ["kms_lighthouse"], connections
    report = create_report(title="KMS agent", user_token=token, org_id=org, data_sources=[ds["id"]])
    return ds["id"], connections[0]["id"], report["id"]


async def _loaded(db, ds_id, report_id):
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    ds = (await db.execute(
        select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.id == ds_id)
    )).scalar_one()
    org = await db.get(Organization, ds.organization_id)
    report = (await db.execute(
        select(Report).where(Report.id == report_id)
        .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
    )).scalar_one()
    user = await db.get(User, report.user_id)
    return ds, org, user, report


async def _planner_context(ds_id, report_id):
    """Exactly what the planner is handed for this user on this agent."""
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        ds, org, user, _ = await _loaded(db, ds_id, report_id)
        ctx = await SchemaContextBuilder(db=db, data_sources=[ds], organization=org, report=None,
                                         user=user).build(with_stats=False)
        return ctx.render() if ctx else ""


def _connection_element(rendered, conn_id):
    match = re.search(r'<connection [^>]*id="' + re.escape(conn_id) + r'"[^>]*>(.*?)</connection>', rendered, re.S)
    assert match, rendered
    return match.group(1)


def test_planner_context_carries_the_kms_browse_note(
    kms, test_client, create_user, login_user, whoami, create_data_source, create_report,
):
    ds_id, conn_id, report_id = _setup(kms, test_client, create_user, login_user, whoami, create_data_source,
                                       create_report)

    element = _connection_element(asyncio.run(_planner_context(ds_id, report_id)), conn_id)

    notes = re.findall(r"<note>\n(.*?)\n</note>", element, re.S)
    assert len(notes) == 1, element
    note = notes[0]
    assert f"returns at most {_MAX_RESULTS} articles and scenarios from the root folder (id {_ROOT})" in note
    assert f"stops after {kc._MAX_FOLDER_CALLS} folder lookups" in note
    # A root-relative example: under the root folder, paths start below it ("Billing/...", not "Procedures/...").
    assert "pass the folder part of an entry's path as folder_id (for example 'Folder/Subfolder')" in note
    assert "paths start below the configured root folder when one is set" in note
    assert "Procedures" not in note
    assert "The root folder limits browsing only: search_files covers everything this identity can see" in note
    assert "There is no catalog" in note
    # Browsing is bounded, not access: no "else denied" scope for this connection.
    assert "anything else is denied" not in element
    assert "<scope>" not in element


async def _agent_run(ds_id, conn_id, report_id, kms, monkeypatch):
    """The agent's file tools, run as the planner runs them, then a rejected re-login."""
    from app.ai.tools.implementations.list_files import ListFilesTool
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.search_files import SearchFilesTool
    from app.dependencies import async_session_maker

    async def end(agen):
        last = None
        async for event in agen:
            last = event
        return last.payload["output"]

    async with async_session_maker() as db:
        _, org, user, report = await _loaded(db, ds_id, report_id)
        ctx = {"db": db, "organization": org, "user": user, "report": report}
        mock = kms["mock"]
        out = {"root": await end(ListFilesTool().run_stream({"connection_id": conn_id}, ctx))}
        # The folder part of one listed entry's path, exactly as the planner note says to build it.
        entry_path = next(f["path"] for f in out["root"]["files"] if f["id"] == "item:302")
        out["folder"] = entry_path.rsplit("/", 1)[0]
        out["billing"] = await end(ListFilesTool().run_stream(
            {"connection_id": conn_id, "folder_id": out["folder"], "recursive": True}, ctx))
        out["search"] = await end(SearchFilesTool().run_stream({"connection_id": conn_id, "query": "billing"}, ctx))
        out["search_query"] = dict(mock.STATE["last_search_params"] or {}).get("query")
        out["article"] = await end(ReadFileTool().run_stream({"connection_id": conn_id, "file_id": "item:301"}, ctx))
        out["scenario"] = await end(ReadFileTool().run_stream(
            {"connection_id": conn_id, "file_id": "scenario:21400"}, ctx))
        out["unknown"] = await end(ReadFileTool().run_stream(
            {"connection_id": conn_id, "file_id": "item:999999"}, ctx))
        # The service account's password changes in KMS and its open session ends: the pooled
        # session is refused, the one re-login is rejected, and the agent gets the fixed auth text.
        out["stored_password"] = mock.USERS["svc"]["password"]      # what the app holds for svc
        monkeypatch.setitem(mock.USERS["svc"], "password", "rotated-svc-pass-e2e")
        mock.expire_sessions()
        mock.REQUEST_LOG.clear()
        out["relogin"] = await end(SearchFilesTool().run_stream({"connection_id": conn_id, "query": "refund"}, ctx))
        out["relogin_log"] = list(mock.REQUEST_LOG)
        out["stale_401s"] = mock.STATE["stale_401s"]
        # During the login hold the next call stops before KMS: no dead session is sent again.
        mock.REQUEST_LOG.clear()
        out["held"] = await end(SearchFilesTool().run_stream({"connection_id": conn_id, "query": "refund"}, ctx))
        out["held_log"] = list(mock.REQUEST_LOG)
        return out


def _member_list_files(ds_id, conn_id, report_id):
    """ListFilesTool with no folder, run as the report's user (the member)."""
    from app.ai.tools.implementations.list_files import ListFilesTool
    from app.dependencies import async_session_maker

    async def run():
        async with async_session_maker() as db:
            _, org, user, report = await _loaded(db, ds_id, report_id)
            ctx = {"db": db, "organization": org, "user": user, "report": report}
            events = [e async for e in ListFilesTool().run_stream({"connection_id": conn_id}, ctx)]
            return events[-1].payload

    return asyncio.run(run())


def _by_id(output):
    return {f["id"]: f["path"] for f in output["files"]}


def test_agent_browses_a_folder_by_path_then_reads(
    kms, monkeypatch, test_client, create_user, login_user, whoami, create_data_source, create_report,
):
    ds_id, conn_id, report_id = _setup(kms, test_client, create_user, login_user, whoami, create_data_source,
                                       create_report)

    out = asyncio.run(_agent_run(ds_id, conn_id, report_id, kms, monkeypatch))

    # No folder: the whole root, with folder-name paths (never folder ids) and no folder rows.
    assert out["root"]["success"] is True, out["root"]
    assert _by_id(out["root"]) == _ROOT_ENTRIES
    # The folder part of an entry's path opens that folder: only its entries, that entry included.
    assert out["folder"] == "Billing"
    assert out["billing"]["success"] is True, out["billing"]
    assert _by_id(out["billing"]) == _BILLING_ENTRIES

    # Search is not root-bounded (ADR-4): every online hit svc may see (the mock answers any query with
    # them), 279 under Accounts included; the offline 4410, alice-only 4430 and the folder hit 4440 are not.
    assert out["search_query"] == "billing"
    assert out["search"]["success"] is True, out["search"]
    assert {f["id"] for f in out["search"]["files"]} == {"item:279", "item:301", "scenario:4400", "item:4420"}

    assert out["article"]["success"] is True, out["article"]
    assert "AI text for a billing dispute." in out["article"]["text"]
    assert out["scenario"]["success"] is True, out["scenario"]
    assert "Is the card blocked?" in out["scenario"]["text"]
    assert "Unblock the card" in out["scenario"]["text"]

    assert out["unknown"]["success"] is False, out["unknown"]
    assert kc._MESSAGES["not_found"] in out["unknown"]["error"]
    # The mock's not-found answer carries a KMS exception text; none of it reaches the agent.
    assert "Cannot get property" not in str(out["unknown"])

    relogin = out["relogin"]
    assert relogin["success"] is False, relogin
    assert kc._USERPASS_AUTH in relogin["error"], relogin
    assert "Connect" not in relogin["error"]
    assert out["stored_password"] not in str(relogin) and "rotated-svc-pass-e2e" not in str(relogin)
    prefix = kms["mock"].PREFIX
    # Exactly the pooled session's refused search, then the one re-login (rejected).
    assert out["relogin_log"] == [(None, "GET", prefix + "/search/full"), ("svc", "POST", prefix + "/login")]
    assert out["stale_401s"] == 1

    held = out["held"]
    assert held["success"] is False, held
    assert kc._HELD_AUTH in held["error"], held
    assert out["stored_password"] not in str(held)
    assert out["held_log"] == []


def test_member_without_credentials_is_told_to_connect_by_list_files(
    kms, test_client, create_user, login_user, whoami, create_data_source, create_report,
):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    # A public per-user KMS data source (svc as the connection identity), as in test_kms_lighthouse.py.
    ds = create_data_source(
        name="KMS per-user", type="kms_lighthouse",
        config={"base_url": kms["base_url"], "allow_http": True, "index_mode": "none", "auth_type": "userpass",
                "root_folder_id": _ROOT},
        credentials={"username": "svc", "password": kms["mock"].USERS["svc"]["password"]},
        auth_policy="user_required", user_token=token, org_id=org,
    )
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True},
                           headers=headers).status_code == 200
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    ds_id, conn_id = ds["id"], connections[0]["id"]
    email = f"kms-bob-{uuid.uuid4().hex[:6]}@example.com"
    invited = test_client.post(f"/api/organizations/{org}/members",
                               json={"organization_id": org, "email": email, "role": "member"}, headers=headers)
    assert invited.status_code == 200, invited.json()
    member = create_user(email=email)
    member_token = login_user(member["email"], member["password"])
    member_h = {"Authorization": f"Bearer {member_token}", "X-Organization-Id": org}
    # The member connects, starts a report on the agent (a report only takes a per-user source the
    # member can use), then disconnects: the report keeps the agent, the member has no credentials.
    endpoint = f"/api/connections/{conn_id}/my-credentials"
    saved = test_client.post(endpoint, json={"auth_mode": "userpass", "credentials": {
        "username": "bob", "password": kms["mock"].USERS["bob"]["password"]}}, headers=member_h)
    assert saved.status_code == 200, saved.json()
    report = create_report(title="KMS member", user_token=member_token, org_id=org, data_sources=[ds_id])
    assert [d["id"] for d in report["data_sources"]] == [ds_id], report
    removed = test_client.delete(endpoint, headers=member_h)
    assert removed.status_code in (200, 204), removed.text
    kms["mock"].REQUEST_LOG.clear()

    payload = _member_list_files(ds_id, conn_id, report["id"])

    out = payload["output"]
    assert out["success"] is False, out
    # The client build's credentials-required refusal, as the Connect call to action the other file tools give.
    conn_name = connections[0]["name"]
    assert f"tell the user to Connect '{conn_name}' from the agent selector" in out["error"], out
    assert "Catalog is empty" not in str(payload)
    assert kms["mock"].REQUEST_LOG == [], "no KMS call may run before the member connects"
