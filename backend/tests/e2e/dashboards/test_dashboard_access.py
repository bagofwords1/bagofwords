"""Who opens a dashboard through /d, and who opens its draft (phase 4).

`/d/{id|name}` serves only the published release, under the dashboard's own
sharing: owner, project, then the dashboard's visibility (private, shared
with people and groups, the organization, public). `/d/{id}/draft` serves
the draft to the people who can open the editor: the owner and the
project's members. Sharing a conversation opens neither.

The expected tables are written out per reader rather than derived, so a
change in any rule shows up as a named cell.
"""
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

DASH = "conv_three_dash/dash_sales"

READERS = ["owner", "admin", "member", "direct_share", "group_share", "group_removed",
           "proj_owner", "proj_viewer", "proj_group", "proj_revoked", "other_org", "anonymous"]

# Published dashboard outside any project. "shared" grants direct_share and
# the analysts group (group_share, proj_group; group_removed left it). The
# organization's full admin opens every published dashboard of it, as /r lets
# an admin open every conversation's dashboard (main, #1400).
OUTSIDE_PROJECT = {
    "none": {**dict.fromkeys(READERS, 404), "owner": 200, "admin": 200},
    "shared": {**dict.fromkeys(READERS, 403), "owner": 200, "admin": 200, "direct_share": 200,
               "group_share": 200, "proj_group": 200, "anonymous": 401},
    "internal": {**dict.fromkeys(READERS, 200), "other_org": 403, "anonymous": 401},
    "public": dict.fromkeys(READERS, 200),
}
# Inside the private project, whoever can view the project opens it whatever
# its visibility: the project's owner, its viewer, the members of the analysts
# group (which holds a view grant on it: proj_group and group_share), and the
# org's full admin (who can view every project).
PROJECT_READERS = {"proj_owner", "proj_viewer", "proj_group", "group_share", "admin"}


def _expected(visibility, in_project):
    table = dict(OUTSIDE_PROJECT[visibility])
    if in_project:
        table.update(dict.fromkeys(PROJECT_READERS, 200))
    return table


def _codes(test_client, corpus, path):
    return {r: test_client.get(path, headers=as_(corpus, r)).status_code for r in READERS}


@pytest.mark.e2e
@pytest.mark.parametrize("in_project", [False, True], ids=["outside-project", "in-project"])
def test_published_dashboard_opens_for_exactly_its_readers(corpus, corpus_rows, test_client, in_project):
    aid = own(corpus_rows, corpus, DASH, project="private" if in_project else None)
    publish(aid)
    mismatches = {}
    for visibility in ("none", "shared", "internal", "public"):
        if visibility == "shared":
            resp = share(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"])
        else:
            resp = share(test_client, corpus, aid, visibility)
        assert resp.status_code == 200, resp.text
        got, want = _codes(test_client, corpus, f"/api/d/{aid}"), _expected(visibility, in_project)
        mismatches.update({f"{visibility}/{r}": (got[r], want[r]) for r in READERS if got[r] != want[r]})
    assert not mismatches, mismatches


@pytest.mark.e2e
@pytest.mark.parametrize("in_project", [False, True], ids=["outside-project", "in-project"])
def test_draft_opens_only_for_the_owner_and_the_project(corpus, corpus_rows, test_client, in_project):
    aid = own(corpus_rows, corpus, DASH, project="private" if in_project else None)
    publish(aid)
    assert share(test_client, corpus, aid, "public").status_code == 200
    want = dict.fromkeys(READERS, 404)
    want.update({"owner": 200, "anonymous": 401})
    if in_project:
        want.update(dict.fromkeys(PROJECT_READERS, 200))
    assert _codes(test_client, corpus, f"/api/d/{aid}/draft") == want


@pytest.mark.e2e
def test_an_unpublished_dashboard_tells_only_its_editors_that_it_is_unpublished(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH, project="private")
    for reader in READERS:
        resp = test_client.get(f"/api/d/{aid}", headers=as_(corpus, reader))
        assert resp.status_code == 404, (reader, resp.text)
        code = resp.json()["error_code"]
        if reader in {"owner"} | PROJECT_READERS:
            assert code == "dashboard.not_published", reader
        else:
            assert code == "artifact.not_found", reader


@pytest.mark.e2e
def test_a_release_that_is_not_ready_is_not_served(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid, status="failed")
    corpus_rows.update("artifacts", aid, visibility="public")  # the API refuses to share it
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member")).status_code == 404
    owner = test_client.get(f"/api/d/{aid}", headers=as_(corpus, "owner"))
    assert owner.status_code == 404 and owner.json()["error_code"] == "dashboard.not_published"


@pytest.mark.e2e
def test_a_conversation_share_opens_no_dashboard(corpus, corpus_rows, test_client):
    """`member` reads the conversation (a conversation share), while the
    dashboard is shared with direct_share only: neither door opens for them."""
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    # Conversation sharing keeps its own API; the row is what that API writes.
    corpus_rows.insert("report_shares", report_id=corpus.reports["conv_three_dash"],
                       user_id=corpus.cast.id("member"), share_type="conversation")
    corpus_rows.update("reports", corpus.reports["conv_three_dash"], conversation_visibility="shared")
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member")).status_code == 403
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "member")).status_code == 404


@pytest.mark.e2e
@pytest.mark.parametrize("person, visibility", [("direct_share", "shared"), ("member", "internal"),
                                                ("owner", "internal")])
def test_leaving_the_organization_closes_the_dashboard(corpus, corpus_rows, test_client, multi_org,
                                                         person, visibility):
    """A grant, the organization's visibility and ownership itself count only
    while their holder is still in the organization. With several
    organizations allowed the account stays active after the removal, so it
    is the dashboard that refuses, not the login."""
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    users = ["direct_share"] if visibility == "shared" else None
    assert share(test_client, corpus, aid, visibility, users=users).status_code == 200
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, person)).status_code == 200
    membership = corpus.cast.people[person].membership_id
    resp = test_client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin"))
    assert resp.status_code == 204, resp.text
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, person)).status_code == 403
    if person == "owner":
        assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, person)).status_code == 404


@pytest.mark.e2e
def test_monitoring_access_is_checked_against_the_release(corpus, corpus_rows, test_client):
    """A dashboard over run history (bow) stays closed to members without the
    console scope, even when shared with the whole organization."""
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_scoped")  # managing it needs the scope too
    publish(aid)
    assert share(test_client, corpus, aid, "internal", by="bow_scoped").status_code == 200
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "admin")).status_code == 200
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member")).status_code == 403
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "bow_unscoped")).status_code == 403


@pytest.mark.e2e
def test_a_dashboard_opens_by_its_name_like_by_its_id(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    corpus_rows.update("artifacts", aid, slug="sales-2026")  # naming is phase 8
    for reader, code in (("direct_share", 200), ("member", 403), ("anonymous", 401)):
        by_name = test_client.get("/api/d/sales-2026", headers=as_(corpus, reader))
        assert by_name.status_code == code, reader
    assert test_client.get("/api/d/sales-2026", headers=as_(corpus, "direct_share")).json()["id"] == aid
    assert test_client.get("/api/d/no-such-name", headers=as_(corpus, "owner")).status_code == 404


@pytest.mark.e2e
def test_a_deleted_dashboard_is_gone_for_everyone(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "public").status_code == 200
    corpus_rows.update("artifacts", aid, deleted_at=corpus_rows.clock())
    for reader in ("owner", "member", "anonymous"):
        assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, reader)).status_code == 404, reader
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).status_code == 404
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "anonymous")).status_code == 401


@pytest.mark.e2e
def test_a_viewer_gets_what_the_page_needs_and_none_of_the_settings(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    release = publish(aid)
    corpus_rows.update("artifacts", aid, notification_subscribers=[{"type": "email", "id": "boss@example.com"}],
                       cron_schedule="0 8 * * *", chat_data_source_ids=[corpus.cast.data_sources["chinook"]])
    assert share(test_client, corpus, aid, "public").status_code == 200
    body = test_client.get(f"/api/d/{aid}").json()
    assert body["id"] == aid and body["release_id"] == release and body["mode"] == "page"
    assert set(body) <= {"id", "slug", "title", "mode", "release_id", "published_at", "last_run_at",
                         "theme_name", "theme_overrides", "include_data_tab", "chat_enabled", "general",
                         # what /r's page reads besides the dashboard (phase 8)
                         "is_owner", "fork_eligibility", "refresh_on_view", "data_sources",
                         # whether this reader's exports hold their own rows (a yes/no)
                         "exports_own_rows"}
    assert "boss@example.com" not in str(body) and "0 8 * * *" not in str(body)
    assert corpus.cast.data_sources["chinook"] not in str(body)


@pytest.mark.e2e
def test_a_visibility_this_code_does_not_know_opens_to_no_one_but_its_editors(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    corpus_rows.update("artifacts", aid, visibility="everyone")  # not a value the API accepts
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "owner")).status_code == 200
    for reader in ("member", "direct_share", "anonymous"):
        assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, reader)).status_code == 404, reader


@pytest.mark.e2e
def test_the_conversations_own_dashboard_sharing_opens_nothing_on_d(corpus, corpus_rows, test_client):
    """Until the data migration moves it, the conversation-level dashboard
    sharing (/r) stays where it is; /d reads only the dashboard's."""
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    report = corpus.reports["conv_three_dash"]
    corpus_rows.insert("report_shares", report_id=report, user_id=corpus.cast.id("direct_share"),
                       share_type="artifact")
    for conversation_visibility in ("shared", "internal", "public"):
        corpus_rows.update("reports", report, artifact_visibility=conversation_visibility)
        for reader in ("direct_share", "member", "anonymous"):
            resp = test_client.get(f"/api/d/{aid}", headers=as_(corpus, reader))
            assert resp.status_code == 404, (conversation_visibility, reader)


@pytest.mark.e2e
def test_a_deleted_project_makes_no_one_an_editor(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH, project="deleted")
    publish(aid)
    for door in (f"/api/d/{aid}", f"/api/d/{aid}/draft"):
        assert test_client.get(door, headers=as_(corpus, "proj_viewer")).status_code == 404, door
        assert test_client.get(door, headers=as_(corpus, "owner")).status_code == 200, door


@pytest.mark.e2e
def test_a_service_account_reads_like_the_organization_check_says(corpus, corpus_rows, test_client):
    """An API-key principal has no membership: 'internal' refuses it, as the
    conversation check does; 'public' lets everyone in."""
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "service_account")).status_code == 403
    assert share(test_client, corpus, aid, "public").status_code == 200
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "service_account")).status_code == 200
