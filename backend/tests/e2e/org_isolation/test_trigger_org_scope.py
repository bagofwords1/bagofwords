"""Triggers belong to one organization and run only for its members.

Contract:

* A trigger is reachable only through the organization it was created in,
  even by its owner.
* A delivery runs as the trigger's owner, so it runs only while the owner is
  an active member of that organization; otherwise it is skipped and the
  trigger is left as it is. Removing a member switches their triggers off.
* A chat identity can only be linked to a member of the organization that
  issued the link.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker

from ._shared_member import (  # noqa: F401
    h, invite, is_member, run, set_user_active, shared_member, two_orgs,
)


def _create_trigger(test_client, token, org_id):
    resp = test_client.post("/api/triggers", headers=h(token, org_id), json={
        "name": f"trigger {uuid.uuid4().hex[:6]}", "source": "generic", "auth_mode": "token",
        "classify_enabled": False, "task_template": "Summarize the event.",
        "mode": "chat", "data_source_ids": [],
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("call", ["get", "runs", "update", "rotate", "delete"])
def test_trigger_is_reachable_only_through_its_own_organization(test_client, shared_member, call):
    o = shared_member
    trigger = _create_trigger(test_client, o["shared_token"], o["b_org"])
    base = f"/api/triggers/{trigger['id']}"

    def _do(org_id):
        headers = h(o["shared_token"], org_id)
        if call == "get":
            return test_client.get(base, headers=headers)
        if call == "runs":
            return test_client.get(f"{base}/runs", headers=headers)
        if call == "update":
            return test_client.put(base, json={"name": "renamed"}, headers=headers)
        if call == "rotate":
            return test_client.post(f"{base}/rotate", headers=headers)
        return test_client.delete(base, headers=headers)

    # Same owner, signed in to their other organization: the trigger is not there.
    assert _do(o["a_org"]).status_code == 404
    still = test_client.get(base, headers=h(o["shared_token"], o["b_org"]))
    assert still.status_code == 200 and still.json()["name"] == trigger["name"]
    assert still.json()["token"] == trigger["token"]

    assert _do(o["b_org"]).status_code in (200, 204)


@pytest.fixture
def agent_runs(monkeypatch):
    """Record agent runs instead of calling a model (the LLM is the boundary)."""
    from app.models.completion import Completion
    from app.services.completion_service import CompletionService

    calls = []

    async def fake_create_completion(self, db, report_id, completion_data,
                                     current_user, organization, background=False, **kw):
        calls.append({"user_id": str(current_user.id), "organization_id": str(organization.id)})
        db.add(Completion(
            prompt={"content": completion_data.prompt.content}, completion={"content": "done"},
            model="stub", report_id=str(report_id), turn_index=0,
            role="system", status="success", user_id=str(current_user.id),
        ))
        await db.commit()

    monkeypatch.setattr(CompletionService, "create_completion", fake_create_completion)
    return calls


def _deliver(trigger_id):
    from app.services.webhook_service import webhook_service

    asyncio.run(webhook_service.process_delivery(
        trigger_id, {"event": "alert"}, {"x-bow-delivery": uuid.uuid4().hex},
    ))


async def _trigger_state(trigger_id):
    from app.models.webhook import Webhook

    async with async_session_maker() as db:
        wh = await db.get(Webhook, trigger_id)
        return {"is_active": wh.is_active, "last_event": wh.last_event}


async def _trigger_is_active(trigger_id):
    return (await _trigger_state(trigger_id))["is_active"]


def test_delivery_is_skipped_while_the_owner_cannot_act_and_resumes_when_they_can(
    test_client, shared_member, agent_runs,
):
    o = shared_member
    trigger = _create_trigger(test_client, o["shared_token"], o["b_org"])

    _deliver(trigger["id"])
    assert agent_runs == [{"user_id": o["shared_id"], "organization_id": o["b_org"]}]

    # Direct write: the owner's account is deactivated (what a directory
    # deprovision leaves) while the trigger itself is still switched on.
    run(set_user_active(o["shared_id"], False))
    _deliver(trigger["id"])

    # No run, and the trigger is left switched on...
    assert len(agent_runs) == 1
    assert run(_trigger_is_active(trigger["id"])) is True

    # ...so it works again as soon as the owner can act.
    run(set_user_active(o["shared_id"], True))
    _deliver(trigger["id"])
    assert len(agent_runs) == 2


def test_removing_a_member_switches_off_their_triggers_in_that_organization(
    test_client, shared_member, agent_runs,
):
    o = shared_member
    in_a = _create_trigger(test_client, o["shared_token"], o["a_org"])
    in_b = _create_trigger(test_client, o["shared_token"], o["b_org"])
    rows = test_client.get(
        f"/api/organizations/{o['b_org']}/members", headers=h(o["b_token"], o["b_org"]),
    ).json()
    row = next(m for m in rows if m["user_id"] == o["shared_id"])

    removed = test_client.delete(
        f"/api/organizations/{o['b_org']}/members/{row['id']}", headers=h(o["b_token"], o["b_org"]),
    )
    assert removed.status_code == 204, removed.text

    _deliver(in_b["id"])
    assert agent_runs == []
    assert run(_trigger_is_active(in_b["id"])) is False
    # Their trigger in the organization they are still in keeps working.
    _deliver(in_a["id"])
    assert agent_runs == [{"user_id": o["shared_id"], "organization_id": o["a_org"]}]


async def _pending_chat_link(org_id):
    # Direct write: a verification link is minted when an unknown chat user
    # messages a connected workspace; there is no API that creates one.
    from app.models.external_platform import ExternalPlatform
    from app.models.external_user_mapping import ExternalUserMapping

    token = uuid.uuid4().hex
    async with async_session_maker() as db:
        platform = ExternalPlatform(organization_id=org_id, platform_type="whatsapp", platform_config={})
        db.add(platform)
        await db.flush()
        db.add(ExternalUserMapping(
            organization_id=org_id, platform_id=platform.id, platform_type="whatsapp",
            external_user_id=f"wa-{uuid.uuid4().hex[:8]}", is_verified=False,
            verification_token=token, verification_expires_at=datetime.utcnow() + timedelta(hours=1),
        ))
        await db.commit()
    return token


async def _link_owner(token_or_none, org_id):
    from sqlalchemy import select

    from app.models.external_user_mapping import ExternalUserMapping

    async with async_session_maker() as db:
        m = (await db.execute(
            select(ExternalUserMapping).where(ExternalUserMapping.organization_id == org_id)
        )).scalars().first()
        return m.app_user_id, m.is_verified


def test_chat_identity_link_can_only_be_completed_by_a_member(test_client, two_orgs):
    o = two_orgs
    token = run(_pending_chat_link(o["b_org"]))
    url = f"/api/settings/integrations/verify/{token}/complete"

    outsider = test_client.post(url, headers={"Authorization": f"Bearer {o['a_member_token']}"})
    assert outsider.status_code == 200 and outsider.json()["success"] is False
    assert run(_link_owner(token, o["b_org"])) == (None, False)

    member = test_client.post(url, headers={"Authorization": f"Bearer {o['b_token']}"})
    assert member.status_code == 200 and member.json()["success"] is True
    assert run(_link_owner(token, o["b_org"])) == (o["b_id"], True)
