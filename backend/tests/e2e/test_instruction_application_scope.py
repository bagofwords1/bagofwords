"""Instructions scoped to connected applications.

The contract:

* an instruction lists a connected application in ``applicable_channels`` as
  ``connected_app:<id>``, next to the fixed channels (multi-select);
* it loads only in conversations that came through that app, wherever the
  conversation is continued (the report's origin stamp decides, not the
  request), and never in plain in-app chat or another app;
* unscoped instructions, and instructions scoped to the conversation's
  delivery channel, keep loading in app conversations;
* an app id from another organization (or none at all) is rejected; one of
  a deleted app is dropped on save so the instruction still saves;
* the picker endpoint lists the org's apps, disabled ones flagged, deleted
  ones left out (who may call it: tests/e2e/rbac/test_instruction_application_targets.py).
"""
import uuid

import pytest

from tests.e2e.test_oauth_app import _allow_multiple_orgs, _issue_token  # noqa: F401 (fixture)

pytestmark = pytest.mark.e2e

PREFIX = "connected_app:"


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return token, whoami(token)["organizations"][0]["id"]


def _app(test_client, headers, name=None):
    r = test_client.post("/api/connected_applications", headers=headers,
                         json={"name": name or f"App {uuid.uuid4().hex[:6]}", "agent_ids": None})
    assert r.status_code == 201, r.text
    return r.json()


def _instruction(test_client, headers, channels, text=None):
    r = test_client.post("/api/instructions/global", headers=headers, json={
        "text": text or f"Rule {uuid.uuid4().hex}", "status": "published", "load_mode": "always",
        "applicable_channels": channels,
    })
    assert r.status_code == 200, r.text
    return r.json()


async def _loaded(org_id, *, report=None, application_id=None):
    """Instruction ids the agent's context would load for this conversation."""
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
    from app.ai.context.context_hub import ContextHub
    from app.dependencies import async_session_maker

    from app.models.organization import Organization

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        if report is not None:
            # The agent's own path: prime_static, including its process-wide
            # instructions cache (shared across conversations).
            hub = ContextHub(db=db, organization=org, report=report, data_sources=[], user=None)
            await hub.prime_static(query="hello")
            section = hub.get_view().static.instructions
        else:
            section = await InstructionContextBuilder(
                db, org, channel="app", application_id=application_id).build(query="hello")
    return {it.id for it in section.items}


@pytest.mark.asyncio
async def test_app_scoped_instruction_loads_only_in_that_apps_conversations(
        test_client, create_user, login_user, whoami):
    token, org_id = _admin(create_user, login_user, whoami)
    headers = _auth(token, org_id)
    app_a, app_b = _app(test_client, headers), _app(test_client, headers)

    everywhere = _instruction(test_client, headers, None)
    only_a = _instruction(test_client, headers, [PREFIX + app_a["id"]])
    a_or_b = _instruction(test_client, headers, [PREFIX + app_a["id"], PREFIX + app_b["id"]])
    slack_or_b = _instruction(test_client, headers, ["slack", PREFIX + app_b["id"]])
    web_app = _instruction(test_client, headers, ["app"])

    in_app = await _loaded(org_id)
    in_a = await _loaded(org_id, application_id=app_a["id"])
    in_b = await _loaded(org_id, application_id=app_b["id"])

    assert everywhere["id"] in in_app & in_a & in_b
    # Delivery channel still matches inside an app conversation.
    assert web_app["id"] in in_app & in_a & in_b
    assert only_a["id"] in in_a and only_a["id"] not in in_app | in_b
    assert a_or_b["id"] in in_a & in_b and a_or_b["id"] not in in_app
    assert slack_or_b["id"] in in_b and slack_or_b["id"] not in in_app | in_a


@pytest.mark.asyncio
async def test_conversation_started_through_an_app_keeps_its_instructions(
        test_client, create_user, login_user, whoami):
    """The report's origin stamp decides, so the agent (which builds context
    from the report, possibly in a later request) sees the app's rules."""
    from app.dependencies import async_session_maker
    from app.models.report import Report

    token, org_id = _admin(create_user, login_user, whoami)
    headers = _auth(token, org_id)
    app = _app(test_client, headers)
    client = test_client.post("/api/oauth/clients", headers=headers, json={
        "application_id": app["id"], "name": app["name"],
        "redirect_uris": ["http://localhost:4173/callback"], "scopes": "app", "trusted": True,
    })
    assert client.status_code == 200, client.text
    scoped = _instruction(test_client, headers, [PREFIX + app["id"]])

    app_token = _issue_token(test_client, login_token=token, client=client.json(), scope="app")["access_token"]
    via_app = test_client.post("/api/reports", headers=_auth(app_token, org_id),
                               json={"title": "From the app", "files": [], "data_sources": []})
    assert via_app.status_code == 200, via_app.text
    in_bow = test_client.post("/api/reports", headers=headers,
                              json={"title": "In BOW", "files": [], "data_sources": []})
    assert in_bow.status_code == 200, in_bow.text

    async with async_session_maker() as db:
        app_report = await db.get(Report, via_app.json()["id"])
        bow_report = await db.get(Report, in_bow.json()["id"])
    # Same prompt, back to back, BOW first: a cache entry shared across
    # surfaces would hand the app conversation BOW's list (or the reverse).
    assert scoped["id"] not in await _loaded(org_id, report=bow_report)
    assert scoped["id"] in await _loaded(org_id, report=app_report)
    assert scoped["id"] not in await _loaded(org_id, report=bow_report)


def test_app_channels_are_validated_and_deleted_apps_dropped(
        test_client, create_user, login_user, whoami, create_organization, _allow_multiple_orgs):
    token, org_id = _admin(create_user, login_user, whoami)
    headers = _auth(token, org_id)
    keep, doomed = _app(test_client, headers), _app(test_client, headers)

    other_org = create_organization(name=f"Other {uuid.uuid4().hex[:6]}", user_token=token)
    foreign = _app(test_client, _auth(token, other_org))

    for bad in (PREFIX + foreign["id"], PREFIX + str(uuid.uuid4())):
        r = test_client.post("/api/instructions/global", headers=headers, json={
            "text": "x", "status": "published", "applicable_channels": ["slack", bad]})
        assert r.status_code == 400, r.text
        assert r.json()["error_code"] == "INSTRUCTION_APPLICATIONS_INVALID"

    inst = _instruction(test_client, headers, ["slack", PREFIX + keep["id"], PREFIX + doomed["id"]])
    assert test_client.delete(f"/api/connected_applications/{doomed['id']}", headers=headers).status_code == 204

    # The editor sends the scope back unchanged with an unrelated edit.
    r = test_client.put(f"/api/instructions/{inst['id']}", headers=headers, json={
        "text": "edited", "applicable_channels": inst["applicable_channels"]})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["applicable_channels"]) == sorted(["slack", PREFIX + keep["id"]])

    # An edit that doesn't touch the scope leaves it alone.
    r = test_client.put(f"/api/instructions/{inst['id']}", headers=headers, json={"text": "again"})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["applicable_channels"]) == sorted(["slack", PREFIX + keep["id"]])


def test_application_targets_list_the_orgs_apps(
        test_client, create_user, login_user, whoami, create_organization, _allow_multiple_orgs):
    token, org_id = _admin(create_user, login_user, whoami)
    headers = _auth(token, org_id)
    live, off, gone = _app(test_client, headers), _app(test_client, headers), _app(test_client, headers)
    assert test_client.patch(f"/api/connected_applications/{off['id']}", headers=headers,
                             json={"enabled": False}).status_code == 200
    assert test_client.delete(f"/api/connected_applications/{gone['id']}", headers=headers).status_code == 204
    other_org = create_organization(name=f"Other {uuid.uuid4().hex[:6]}", user_token=token)
    _app(test_client, _auth(token, other_org))

    r = test_client.get("/api/instructions/application-targets", headers=headers)
    assert r.status_code == 200, r.text
    by_id = {a["id"]: a for a in r.json()}
    assert set(by_id) == {live["id"], off["id"]}
    assert by_id[live["id"]]["enabled"] is True and by_id[off["id"]]["enabled"] is False
    assert "agent_ids" in by_id[live["id"]]
