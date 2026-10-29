"""Every dashboard collaboration route asks for the right capability
(collab01, QA section G): an owner and an editor get through; a viewer, a
member with no grant, a stranger from another organization and anonymous do
not -- and a refused call changes nothing.

The world: `owner` owns the dashboard, `member` is its editor, `direct_share`
its viewer, `admin` an org admin with no grant (who may only read and hand
the dashboard over: dashboard_access.capabilities), `other_org` a stranger.
"""
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

DASH = "conv_three_dash/dash_sales"
ZERO = "00000000-0000-0000-0000-000000000000"
DENIED = {"direct_share": {403, 404}, "admin": {403, 404}, "other_org": {403, 404}, "anonymous": {401}}


def _routes(aid, version_id, release_id, report_id, member_id):
    """(method, url, body) for each gated route, with real ids so a refusal
    can only come from the gate, never from a missing row."""
    return [
        ("GET", f"/api/artifacts/{aid}/history", None),
        ("GET", f"/api/artifacts/{aid}/drafts", None),
        ("POST", f"/api/artifacts/{aid}/drafts", {"name": "gate check"}),
        ("PUT", f"/api/artifacts/{aid}/versions/{version_id}/name", {"name": "gate check"}),
        ("GET", f"/api/artifacts/{aid}/open-state?report_id={report_id}", None),
        ("POST", f"/api/artifacts/{aid}/bring", {"report_id": report_id}),
        ("POST", f"/api/artifacts/{aid}/restore", {"release_id": release_id}),
        ("POST", f"/api/artifacts/{aid}/transfer", {"user_id": member_id}),
        ("POST", f"/api/artifacts/{aid}/publish", None),
        ("GET", f"/api/artifacts/{aid}/publish", None),
        ("GET", f"/api/artifacts/{aid}/settings", None),
        ("PUT", f"/api/artifacts/{aid}/settings", {"notes": "gate check"}),
        ("GET", f"/api/artifacts/{aid}/presence", None),
        ("PUT", f"/api/artifacts/{aid}/presence", {}),
    ]


def _snapshot(test_client, corpus, aid):
    owner = as_(corpus, "owner")
    history = test_client.get(f"/api/artifacts/{aid}/history", headers=owner).json()
    return (
        test_client.get(f"/api/artifacts/{aid}/sharing", headers=owner).json(),
        test_client.get(f"/api/artifacts/{aid}/settings", headers=owner).json(),
        sorted((v["id"], v.get("name")) for v in history["versions"]),
        sorted(c["report_id"] for c in history["conversations"]),
        test_client.get(f"/api/artifacts/{aid}/publish", headers=owner).json().get("published"),
    )


@pytest.mark.e2e
def test_only_owner_and_editors_reach_the_collaboration_routes(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    release_id = publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    owner = as_(corpus, "owner")
    granted = test_client.put(f"/api/artifacts/{aid}/sharing", headers=owner, json={
        "visibility": "shared", "user_ids": [corpus.cast.id("direct_share")],
        "editor_user_ids": [corpus.cast.id("member")]})
    assert granted.status_code == 200, granted.text
    version_id = test_client.get(f"/api/artifacts/{aid}/history", headers=owner).json()["versions"][0]["id"]
    report_id = corpus.reports["conv_three_dash"]
    routes = _routes(aid, version_id, release_id, report_id, corpus.cast.id("member"))

    before = _snapshot(test_client, corpus, aid)
    for person, codes in DENIED.items():
        for method, url, body in routes:
            if person == "admin" and url.endswith("/transfer"):
                continue  # the admin's rescue for a departed owner; checked below
            resp = test_client.request(method, url, json=body, headers=as_(corpus, person))
            assert resp.status_code in codes, (person, method, url, resp.status_code, resp.text)
            if person in ("direct_share", "admin"):
                # A code the frontend can localize, never a bare message.
                assert resp.json().get("error_code"), (person, method, url, resp.text)
    assert _snapshot(test_client, corpus, aid) == before

    # The editor gets past the gate on the reads (what the toolbar loads).
    member = as_(corpus, "member")
    for url in (f"/api/artifacts/{aid}/history", f"/api/artifacts/{aid}/publish",
                f"/api/artifacts/{aid}/settings", f"/api/artifacts/{aid}/presence"):
        assert test_client.get(url, headers=member).status_code == 200, url
    # ...but transfer stays the owner's.
    resp = test_client.post(f"/api/artifacts/{aid}/transfer", json={"user_id": corpus.cast.id("member")},
                            headers=member)
    assert resp.status_code == 403 and resp.json()["error_code"]
    # The org admin may hand it over (to an editor).
    resp = test_client.post(f"/api/artifacts/{aid}/transfer", json={"user_id": corpus.cast.id("member")},
                            headers=as_(corpus, "admin"))
    assert resp.status_code == 200 and resp.json()["owner_id"] == corpus.cast.id("member")


@pytest.mark.e2e
def test_an_unknown_dashboard_is_not_found_for_everyone_signed_in(corpus, test_client):
    for person in ("owner", "member", "admin"):
        for method, url, body in _routes(ZERO, ZERO, ZERO, ZERO, ZERO):
            resp = test_client.request(method, url, json=body, headers=as_(corpus, person))
            assert resp.status_code == 404, (person, method, url, resp.status_code)
            assert resp.json()["error_code"] == "artifact.not_found"
