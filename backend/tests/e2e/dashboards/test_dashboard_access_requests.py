"""Request-to-view a dashboard, answered by its editors (access requests).

A member who opens a published dashboard they cannot see asks for view access
from the no-access page; the dashboard's editors approve or deny it from the
share settings. Covers the product rules: an editor approves, a viewer/admin
cannot, a duplicate is refused, the 7-day wait after a denial, the org setting
off, a dashboard with no active editors, a private dashboard switching to
"Specific people", and the title never leaking to the blocked page.
"""
import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

DASH = "conv_three_dash/dash_sales"


# ── helpers ───────────────────────────────────────────────────────────────

def _blocked(test_client, corpus, aid, by):
    return test_client.get(f"/api/artifacts/{aid}/access_request", headers=as_(corpus, by))


def _request(test_client, corpus, aid, by, note=None):
    body = {"note": note} if note is not None else {}
    return test_client.post(f"/api/artifacts/{aid}/access_requests", json=body, headers=as_(corpus, by))


def _list(test_client, corpus, aid, by):
    return test_client.get(f"/api/artifacts/{aid}/access_requests", headers=as_(corpus, by))


def _approve(test_client, corpus, rid, by):
    return test_client.post(f"/api/access_requests/{rid}/approve", headers=as_(corpus, by))


def _deny(test_client, corpus, rid, by):
    return test_client.post(f"/api/access_requests/{rid}/deny", headers=as_(corpus, by))


def _set_editor(test_client, corpus, aid, *, editor_users=None, editor_groups=None, viewers=None,
                visibility="shared", by="owner"):
    body = {"visibility": visibility}
    if editor_users is not None:
        body["editor_user_ids"] = [corpus.cast.id(u) for u in editor_users]
    if editor_groups is not None:
        body["editor_group_ids"] = [corpus.cast.groups[g] for g in editor_groups]
    if viewers is not None:
        body["user_ids"] = [corpus.cast.id(u) for u in viewers]
    return test_client.put(f"/api/artifacts/{aid}/sharing", json=body, headers=as_(corpus, by))


def _set_setting(test_client, corpus, key, value):
    return test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                           json={"config": {key: {"value": value}}})


def _notif_types(test_client, corpus, person):
    resp = test_client.get("/api/notifications", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    items = resp.json()
    items = items.get("items", items) if isinstance(items, dict) else items
    return [n.get("type") for n in items]


# ── the flow ────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_an_editor_approves_and_the_member_gains_access(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200

    # The member is blocked and can request.
    before = _blocked(test_client, corpus, aid, "member").json()
    assert before["has_approvers"] is True and before["pending"] is False
    assert "title" not in before  # never leak the title

    assert _request(test_client, corpus, aid, "member", note="Need it for Q3").status_code == 200
    assert _blocked(test_client, corpus, aid, "member").json()["pending"] is True

    # The owner (an editor) sees it and the requester's note.
    listing = _list(test_client, corpus, aid, "owner").json()
    assert len(listing["requests"]) == 1
    req = listing["requests"][0]
    assert req["note"] == "Need it for Q3"

    assert _approve(test_client, corpus, req["id"], "owner").status_code == 200
    # Now a viewer: the published dashboard opens.
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member")).status_code == 200
    # The request is closed for everyone.
    assert _list(test_client, corpus, aid, "owner").json()["requests"] == []


@pytest.mark.e2e
def test_the_owner_and_named_editors_are_notified_not_group_editors(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    # direct_share is a named editor; the analysts group is a group editor.
    assert _set_editor(test_client, corpus, aid, editor_users=["direct_share"],
                       editor_groups=["analysts"]).status_code == 200
    assert _request(test_client, corpus, aid, "member").status_code == 200

    assert "access_request" in _notif_types(test_client, corpus, "owner")
    assert "access_request" in _notif_types(test_client, corpus, "direct_share")
    # group_share reaches editing only through the analysts group -> not notified.
    assert "access_request" not in _notif_types(test_client, corpus, "group_share")


@pytest.mark.e2e
def test_approval_notifies_the_requester_a_denial_is_silent(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared").status_code == 200

    assert _request(test_client, corpus, aid, "member").status_code == 200
    rid = _list(test_client, corpus, aid, "owner").json()["requests"][0]["id"]
    assert _approve(test_client, corpus, rid, "owner").status_code == 200
    assert "access_granted" in _notif_types(test_client, corpus, "member")

    # A denial sends the requester nothing.
    assert _request(test_client, corpus, aid, "has_results").status_code == 200
    rid2 = _list(test_client, corpus, aid, "owner").json()["requests"][0]["id"]
    assert _deny(test_client, corpus, rid2, "owner").status_code == 200
    assert "access_granted" not in _notif_types(test_client, corpus, "has_results")


@pytest.mark.e2e
def test_a_viewer_or_admin_cannot_approve(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    assert _request(test_client, corpus, aid, "member").status_code == 200
    rid = _list(test_client, corpus, aid, "owner").json()["requests"][0]["id"]

    # A plain member / a direct viewer / the org admin: none may approve.
    for who in ("member", "direct_share", "admin"):
        assert _approve(test_client, corpus, rid, who).status_code in (403, 404), who
        assert _list(test_client, corpus, aid, who).status_code in (403, 404), who
    # Still pending after the failed attempts.
    assert len(_list(test_client, corpus, aid, "owner").json()["requests"]) == 1


@pytest.mark.e2e
def test_only_one_open_request_per_member(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared").status_code == 200
    assert _request(test_client, corpus, aid, "member").status_code == 200
    dup = _request(test_client, corpus, aid, "member")
    assert dup.status_code == 409
    assert dup.json()["error_code"] == "access_request.pending"


@pytest.mark.e2e
def test_a_denied_member_waits_seven_days(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared").status_code == 200
    assert _request(test_client, corpus, aid, "member").status_code == 200
    rid = _list(test_client, corpus, aid, "owner").json()["requests"][0]["id"]
    assert _deny(test_client, corpus, rid, "owner").status_code == 200

    # Right away: refused.
    soon = _request(test_client, corpus, aid, "member")
    assert soon.status_code == 403 and soon.json()["error_code"] == "access_request.too_soon"

    # Eight days later (the denial is backdated): allowed again.
    with corpus_rows.engine.begin() as conn:
        conn.execute(text("UPDATE dashboard_access_requests SET decided_at = :t WHERE id = :i"),
                     {"t": "2020-01-01 00:00:00", "i": rid})
    assert _request(test_client, corpus, aid, "member").status_code == 200


@pytest.mark.e2e
def test_the_setting_off_refuses_new_requests_but_keeps_old_ones(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared").status_code == 200
    # One request in flight before the setting is turned off.
    assert _request(test_client, corpus, aid, "member").status_code == 200

    assert _set_setting(test_client, corpus, "allow_access_requests", False).status_code == 200
    blocked = _blocked(test_client, corpus, aid, "direct_share").json()
    assert blocked["requests_enabled"] is False
    refused = _request(test_client, corpus, aid, "direct_share")
    assert refused.status_code == 403 and refused.json()["error_code"] == "access_request.disabled"

    # The already-sent request is still answerable.
    rid = _list(test_client, corpus, aid, "owner").json()["requests"][0]["id"]
    assert _approve(test_client, corpus, rid, "owner").status_code == 200


@pytest.mark.e2e
def test_no_one_to_approve(corpus, corpus_rows, test_client):
    # Owner is forker; remove their membership -> no active editors, no project.
    aid = own(corpus_rows, corpus, DASH, owner="forker")
    publish(aid)
    cast = corpus.cast
    membership = cast.people["forker"].membership_id
    assert test_client.delete(f"/api/organizations/{cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204

    blocked = _blocked(test_client, corpus, aid, "member").json()
    assert blocked["has_approvers"] is False
    refused = _request(test_client, corpus, aid, "member")
    assert refused.status_code == 403 and refused.json()["error_code"] == "access_request.no_approvers"


@pytest.mark.e2e
def test_approving_a_private_dashboard_switches_it_to_specific_people(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    # Private: visibility 'none'. The owner can still see it; the member cannot.
    assert share(test_client, corpus, aid, "none").status_code == 200

    assert _request(test_client, corpus, aid, "member").status_code == 200
    listing = _list(test_client, corpus, aid, "owner").json()
    assert listing["is_private"] is True  # the dialog says it will switch
    rid = listing["requests"][0]["id"]
    assert _approve(test_client, corpus, rid, "owner").status_code == 200

    with corpus_rows.engine.connect() as conn:
        vis = conn.execute(text("SELECT visibility FROM artifacts WHERE id = :a"), {"a": aid}).scalar()
        grant = conn.execute(
            text("SELECT role FROM artifact_shares WHERE artifact_id = :a AND user_id = :u"),
            {"a": aid, "u": corpus.cast.id("member")}).scalar()
    assert vis == "shared" and grant == "view"
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member")).status_code == 200


@pytest.mark.e2e
def test_the_dialog_warns_when_the_dashboard_runs_as_its_creator(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared").status_code == 200
    # Switch the dashboard to run with the creator's credentials.
    assert test_client.put(f"/api/artifacts/{aid}/settings", headers=as_(corpus, "owner"),
                           json={"shared_run_identity": "creator"}).status_code == 200
    assert _request(test_client, corpus, aid, "member").status_code == 200
    listing = _list(test_client, corpus, aid, "owner").json()
    assert listing["runs_as_creator"] is True
    assert listing["creator_name"]


@pytest.mark.e2e
def test_the_blocked_page_never_sees_the_title(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared").status_code == 200
    # The thin blocked payload carries the owner's name, not the title.
    body = _blocked(test_client, corpus, aid, "member").json()
    assert "title" not in body
    assert body.get("owner_name")  # owner's display name is allowed
    assert set(body) <= {"owner_name", "requests_enabled", "eligible", "has_approvers",
                         "pending", "retry_at"}
    # And fetching the dashboard itself as the blocked member reveals no title.
    meta = test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member"))
    assert meta.status_code in (403, 404)
    assert "title" not in (meta.json() or {})
