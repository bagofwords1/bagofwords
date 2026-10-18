"""The dashboard's owner shares it, on the dashboard itself (phase 4).

`PUT /api/dashboards/{id}/sharing` sets one dashboard's visibility and its
people and groups. It follows the rules of today's share modal, with one
addition: a dashboard is shared only once it is published. It never touches
the conversation's sharing or another dashboard's.
"""
import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

DASH = "conv_three_dash/dash_sales"
SIBLING = "conv_three_dash/dash_stock"


def _sharing(test_client, corpus, aid, by="owner"):
    return test_client.get(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, by))


@pytest.mark.e2e
def test_only_the_owner_reads_and_changes_the_sharing(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert _sharing(test_client, corpus, aid).status_code == 200
    assert share(test_client, corpus, aid, "internal").status_code == 200
    for reader, codes in (("member", {403}), ("admin", {403}), ("direct_share", {403}),
                          ("other_org", {403, 404}), ("anonymous", {401})):
        assert _sharing(test_client, corpus, aid, by=reader).status_code in codes, reader
        assert share(test_client, corpus, aid, "public", by=reader).status_code in codes, reader
    assert _sharing(test_client, corpus, aid).json()["visibility"] == "internal"


@pytest.mark.e2e
def test_a_dashboard_is_shared_only_once_it_is_published(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    for visibility, extra in (("shared", {"users": ["direct_share"]}), ("internal", {}), ("public", {})):
        resp = share(test_client, corpus, aid, visibility, **extra)
        assert resp.status_code == 409, visibility
        assert resp.json()["error_code"] == "dashboard.share_requires_publish"
    assert share(test_client, corpus, aid, "none").status_code == 200
    body = _sharing(test_client, corpus, aid).json()
    assert body == {"visibility": "none", "published": False, "user_ids": [], "group_ids": []}
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200


@pytest.mark.e2e
def test_people_and_groups_are_replaced_kept_or_cleared(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    cast = corpus.cast
    assert share(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"]).status_code == 200
    body = _sharing(test_client, corpus, aid).json()
    assert body["published"] is True and body["visibility"] == "shared"
    assert body["user_ids"] == [cast.id("direct_share")] and body["group_ids"] == [cast.groups["analysts"]]

    # A list left out stays as it is.
    assert share(test_client, corpus, aid, "shared", users=["member"]).status_code == 200
    body = _sharing(test_client, corpus, aid).json()
    assert body["user_ids"] == [cast.id("member")] and body["group_ids"] == [cast.groups["analysts"]]

    # Leaving "shared" clears every grant; coming back starts empty.
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert share(test_client, corpus, aid, "shared").status_code == 200
    body = _sharing(test_client, corpus, aid).json()
    assert (body["user_ids"], body["group_ids"]) == ([], [])


@pytest.mark.e2e
def test_grants_name_people_and_groups_of_the_organization_only(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    outsider = share(test_client, corpus, aid, "shared", users=["other_org"])
    assert outsider.status_code == 400 and outsider.json()["error_code"] == "user.not_found"
    resp = test_client.put(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, "owner"),
                           json={"visibility": "shared", "group_ids": ["00000000-0000-0000-0000-000000000000"]})
    assert resp.status_code == 400 and resp.json()["error_code"] == "group.not_found"
    bad = test_client.put(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, "owner"),
                          json={"visibility": "everyone"})
    assert bad.status_code == 422
    assert _sharing(test_client, corpus, aid).json()["visibility"] == "none"


@pytest.mark.e2e
def test_a_monitoring_dashboard_is_never_public(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_scoped")
    publish(aid)
    assert share(test_client, corpus, aid, "public", by="bow_scoped").status_code == 403
    assert share(test_client, corpus, aid, "internal", by="bow_scoped").status_code == 200


@pytest.mark.e2e
def test_sharing_one_dashboard_leaves_the_conversation_and_the_others_alone(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    sibling = own(corpus_rows, corpus, SIBLING)
    publish(aid)
    report = corpus.reports["conv_three_dash"]

    def state():
        with corpus_rows.engine.connect() as conn:
            r = conn.execute(text("SELECT artifact_visibility, conversation_visibility, status FROM reports "
                                  "WHERE id = :r"), {"r": report}).one()
            shares = conn.execute(text("SELECT count(*) FROM report_shares WHERE report_id = :r"),
                                  {"r": report}).scalar()
            sib = conn.execute(text("SELECT visibility FROM artifacts WHERE id = :a"), {"a": sibling}).scalar()
            sib_shares = conn.execute(text("SELECT count(*) FROM artifact_shares WHERE artifact_id = :a"),
                                      {"a": sibling}).scalar()
        return tuple(r), shares, sib, sib_shares

    before = state()
    assert share(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"]).status_code == 200
    assert share(test_client, corpus, aid, "public").status_code == 200
    assert state() == before


@pytest.mark.e2e
def test_new_readers_are_notified_with_the_dashboard_link(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"]).status_code == 200

    def share_links(reader):
        resp = test_client.get("/api/notifications", headers=as_(corpus, reader))
        assert resp.status_code == 200, resp.text
        items = resp.json()
        items = items.get("items", items) if isinstance(items, dict) else items
        return [n["link"] for n in items if n.get("type") == "share_artifact"]

    assert share_links("direct_share") == [f"/d/{aid}"]
    assert share_links("group_share") == [f"/d/{aid}"]  # a member of the shared group
    assert share_links("member") == []
    # Saving again with the same people notifies no one twice.
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    assert share_links("direct_share") == [f"/d/{aid}"]


@pytest.mark.e2e
def test_the_list_shows_grants_that_still_count_and_saves_back(corpus, corpus_rows, test_client, multi_org):
    """Someone who left the organization keeps their row but not their access;
    the owner's list leaves them out, so saving it back (plus a new person)
    works, and the save drops the old row."""
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    cast = corpus.cast
    assert share(test_client, corpus, aid, "shared", users=["direct_share", "member"]).status_code == 200
    membership = cast.people["direct_share"].membership_id
    assert test_client.delete(f"/api/organizations/{cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204
    listed = _sharing(test_client, corpus, aid).json()["user_ids"]
    assert listed == [cast.id("member")]
    resp = test_client.put(f"/api/dashboards/{aid}/sharing", headers=as_(corpus, "owner"),
                           json={"visibility": "shared", "user_ids": listed + [cast.id("group_share")]})
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["user_ids"]) == sorted([cast.id("member"), cast.id("group_share")])
    with corpus_rows.engine.connect() as conn:
        left = conn.execute(text("SELECT count(*) FROM artifact_shares WHERE artifact_id = :a AND user_id = :u"),
                            {"a": aid, "u": cast.id("direct_share")}).scalar()
    assert left == 0


@pytest.mark.e2e
def test_an_owner_without_monitoring_access_cannot_manage_a_monitoring_dashboard(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_unscoped")
    publish(aid)
    assert _sharing(test_client, corpus, aid, by="bow_unscoped").status_code == 403
    assert share(test_client, corpus, aid, "internal", by="bow_unscoped").status_code == 403
    assert test_client.get(f"/api/dashboards/{aid}/settings", headers=as_(corpus, "bow_unscoped")).status_code == 403
