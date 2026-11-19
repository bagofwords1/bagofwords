"""Each organization has its own inbox for a person.

Contract: listing, counting, reading and dismissing notifications only ever
touch the notifications of the organization the request is for.
"""
import uuid

import pytest

from app.dependencies import async_session_maker

from ._shared_member import h, run, shared_member, two_orgs  # noqa: F401


async def _notify(org_id, user_id, title):
    # Direct write: producing a notification through the API needs a share or
    # a scheduled run; the row itself is all this contract is about.
    from app.models.notification import Notification

    async with async_session_maker() as db:
        n = Notification(
            organization_id=org_id, user_id=user_id, source="schedule",
            type="scheduled_run", severity="info", title=title,
        )
        db.add(n)
        await db.commit()
        return str(n.id)


@pytest.fixture
def inboxes(shared_member):
    o = shared_member
    a_title, b_title = f"a-{uuid.uuid4().hex[:8]}", f"b-{uuid.uuid4().hex[:8]}"
    return {
        **o,
        "a_title": a_title, "b_title": b_title,
        "a_notification": run(_notify(o["a_org"], o["shared_id"], a_title)),
        "b_notification": run(_notify(o["b_org"], o["shared_id"], b_title)),
        "b_second": run(_notify(o["b_org"], o["shared_id"], b_title + "-2")),
    }


def _titles(test_client, token, org_id):
    resp = test_client.get("/api/notifications", headers=h(token, org_id))
    assert resp.status_code == 200, resp.text
    return {i["title"] for i in resp.json()["items"]}


def _unread(test_client, token, org_id):
    return test_client.get("/api/notifications/count", headers=h(token, org_id)).json()["unread"]


def test_list_and_count_show_only_the_requested_organization(test_client, inboxes):
    o = inboxes
    assert _titles(test_client, o["shared_token"], o["a_org"]) == {o["a_title"]}
    assert _titles(test_client, o["shared_token"], o["b_org"]) == {o["b_title"], o["b_title"] + "-2"}
    assert _unread(test_client, o["shared_token"], o["a_org"]) == 1
    assert _unread(test_client, o["shared_token"], o["b_org"]) == 2


@pytest.mark.parametrize("action", ["read", "dismiss"])
def test_a_notification_can_only_be_changed_from_its_own_organization(test_client, inboxes, action):
    o = inboxes
    url = f"/api/notifications/{o['b_notification']}/{action}"
    body = {"read": True} if action == "read" else None

    from_a = test_client.post(url, json=body, headers=h(o["shared_token"], o["a_org"]))
    assert from_a.status_code == 200 and from_a.json()["ok"] is False
    assert _unread(test_client, o["shared_token"], o["b_org"]) == 2

    from_b = test_client.post(url, json=body, headers=h(o["shared_token"], o["b_org"]))
    assert from_b.status_code == 200 and from_b.json()["ok"] is True
    assert _unread(test_client, o["shared_token"], o["b_org"]) == 1


def test_read_all_leaves_other_organizations_unread(test_client, inboxes):
    o = inboxes
    resp = test_client.post("/api/notifications/read-all", json={}, headers=h(o["shared_token"], o["a_org"]))

    assert resp.status_code == 200 and resp.json()["marked"] == 1
    assert _unread(test_client, o["shared_token"], o["a_org"]) == 0
    assert _unread(test_client, o["shared_token"], o["b_org"]) == 2
