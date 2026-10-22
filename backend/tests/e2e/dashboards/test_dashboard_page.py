"""What the /d page needs to look like /r (phase 8, part A).

The /d page is the conversation's /r page served from the dashboard, so its
meta carries what /r's page reads besides the dashboard itself: whether the
reader is the owner (and so sees the owner's controls and never runs it as a
viewer), may fork it and why not, whether it refreshes when
opened, and the kinds of connection it runs on (the sign-in gate's icon).

The page's runtime -- forms, records, view counting, the owner's analytics --
lives under /api/artifacts/{id}/runtime and was let in by the conversation's
sharing only: a /d reader the conversation is not shared with, and every
reader once the conversation is deleted, was refused. It now also lets in
whoever the dashboard lets in, and the owner is the dashboard's.
"""
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

CONV = "conv_three_dash"
DASH = f"{CONV}/dash_sales"


def _meta(test_client, corpus, aid, person):
    resp = test_client.get(f"/api/d/{aid}", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _private_conversation(corpus, corpus_rows, conv=CONV):
    """The corpus shares this conversation with the organization; here it
    is shared with nobody, so only the dashboard's sharing can let a reader in."""
    corpus_rows.update("reports", corpus.reports[conv], artifact_visibility="none")


def _published(corpus, corpus_rows, test_client, key=DASH, visibility="internal", project=None):
    _private_conversation(corpus, corpus_rows, key.split("/")[0])
    aid = own(corpus_rows, corpus, key, project=project)
    publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    return aid


# ── the meta ─────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_the_page_knows_who_is_looking(corpus, corpus_rows, test_client):
    aid = _published(corpus, corpus_rows, test_client, visibility="public", project="private")
    seen = {p: _meta(test_client, corpus, aid, p)["is_owner"] for p in ("owner", "proj_viewer", "member", "anonymous")}
    # The project's members may open the draft, but the owner's controls are the owner's.
    assert seen == {"owner": True, "proj_viewer": False, "member": False, "anonymous": False}


@pytest.mark.e2e
def test_the_page_says_whether_and_why_not_a_reader_may_fork(corpus, corpus_rows, test_client):
    aid = _published(corpus, corpus_rows, test_client, visibility="public")
    eligibility = {p: _meta(test_client, corpus, aid, p)["fork_eligibility"]
                   for p in ("member", "anonymous", "other_org")}
    assert eligibility == {"member": {"can_fork": True, "reason": None},
                           "anonymous": {"can_fork": False, "reason": "not_logged_in"},
                           "other_org": {"can_fork": False, "reason": "different_org"}}
    assert test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                           json={"config": {"allow_forks": {"value": False}}}).status_code == 200
    assert _meta(test_client, corpus, aid, "member")["fork_eligibility"] == {"can_fork": False,
                                                                            "reason": "forks_disabled"}


@pytest.mark.e2e
def test_the_pages_answer_matches_what_fork_does(corpus, corpus_rows, test_client, monkeypatch):
    """No permission to create conversations: the page hides Fork, as the
    route refuses it."""
    from app.core import permission_resolver

    aid = _published(corpus, corpus_rows, test_client)
    real = permission_resolver.resolve_permissions

    async def no_create(db, user_id, org_id):
        resolved = await real(db, user_id, org_id)
        resolved.org_permissions = {p for p in resolved.org_permissions
                                    if p not in ("create_reports", "full_admin_access")}
        return resolved

    monkeypatch.setattr(permission_resolver, "resolve_permissions", no_create)
    assert _meta(test_client, corpus, aid, "member")["fork_eligibility"] == {"can_fork": False,
                                                                            "reason": "no_permission"}


@pytest.mark.e2e
def test_the_page_knows_whether_it_refreshes_when_opened(corpus, corpus_rows, test_client):
    aid = _published(corpus, corpus_rows, test_client)
    assert _meta(test_client, corpus, aid, "member")["refresh_on_view"] is True
    corpus_rows.update("artifacts", aid, refresh_on_view=False)
    assert _meta(test_client, corpus, aid, "member")["refresh_on_view"] is False


@pytest.mark.e2e
def test_the_page_gets_the_kinds_of_connection_and_nothing_else(corpus, corpus_rows, test_client):
    aid = _published(corpus, corpus_rows, test_client, key="user_required/dash", visibility="public")
    sources = _meta(test_client, corpus, aid, "anonymous")["data_sources"]
    conns = [c for ds in sources for c in ds["connections"]]
    assert conns and any(c["auth_policy"] == "user_required" for c in conns)
    assert all(set(ds) == {"connections"} for ds in sources)
    assert all(set(c) == {"type", "auth_policy"} for c in conns)


@pytest.mark.e2e
def test_the_draft_page_gets_the_same_page_settings(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    published = test_client.get(f"/api/d/{aid}", headers=as_(corpus, "owner"))
    assert published.status_code == 404  # not published yet
    draft = test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "owner")).json()
    publish(aid)
    assert draft["general"] == _meta(test_client, corpus, aid, "owner")["general"]


# ── the runtime ──────────────────────────────────────────────────────────

def _runtime(test_client, corpus, aid, person):
    return test_client.get(f"/api/artifacts/{aid}/runtime/view-token?surface=standalone",
                           headers=as_(corpus, person)).status_code


@pytest.mark.e2e
def test_a_reader_of_the_dashboard_reaches_its_runtime(corpus, corpus_rows, test_client):
    """The conversation is shared with nobody; the dashboard with the
    organization."""
    aid = _published(corpus, corpus_rows, test_client)
    codes = {p: _runtime(test_client, corpus, aid, p) for p in ("owner", "member", "other_org", "anonymous")}
    # Refused like an unknown id (main, #1304: a 401/403 would confirm the
    # dashboard exists).
    assert codes == {"owner": 200, "member": 200, "other_org": 404, "anonymous": 404}


@pytest.mark.e2e
def test_the_runtime_outlives_the_conversation(corpus, corpus_rows, test_client):
    aid = _published(corpus, corpus_rows, test_client)
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    assert {p: _runtime(test_client, corpus, aid, p) for p in ("owner", "member")} == {"owner": 200, "member": 200}
    # The owner is the dashboard's: their analytics stay theirs.
    analytics = f"/api/artifacts/{aid}/runtime/analytics"
    assert test_client.get(analytics, headers=as_(corpus, "owner")).status_code == 200
    assert test_client.get(analytics, headers=as_(corpus, "member")).status_code == 403


@pytest.mark.e2e
def test_the_runtime_stays_closed_to_whoever_the_dashboard_is_not_shared_with(corpus, corpus_rows, test_client):
    _private_conversation(corpus, corpus_rows)
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)  # shared with nobody
    assert _runtime(test_client, corpus, aid, "member") == 404
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    assert _runtime(test_client, corpus, aid, "member") == 404
    assert _runtime(test_client, corpus, aid, "owner") == 200


@pytest.mark.e2e
def test_an_unpublished_dashboards_runtime_is_the_conversations_as_today(corpus, corpus_rows, test_client):
    """Never published: nothing of the dashboard's sharing applies (it is
    shared only after Publish); the editors still reach it."""
    _private_conversation(corpus, corpus_rows)
    aid = own(corpus_rows, corpus, DASH, project="private")
    corpus_rows.update("artifacts", aid, visibility="internal")
    assert _runtime(test_client, corpus, aid, "member") == 404
    assert _runtime(test_client, corpus, aid, "owner") == 200


# ── review fixes ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_an_owner_who_left_is_not_the_runtimes_owner(corpus, corpus_rows, test_client, multi_org):
    """The dashboard's owner counts as the owner only while still in the
    organization (dashboard_access.counts_as_owner): once removed, a public
    dashboard reads to them as to anyone."""
    aid = _published(corpus, corpus_rows, test_client, visibility="public")
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    analytics = f"/api/artifacts/{aid}/runtime/analytics"
    assert test_client.get(analytics, headers=as_(corpus, "owner")).status_code == 200
    membership = corpus.cast.people["owner"].membership_id
    assert test_client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204
    assert _runtime(test_client, corpus, aid, "owner") == 200  # public
    assert test_client.get(analytics, headers=as_(corpus, "owner")).status_code == 403


@pytest.mark.e2e
def test_an_editor_without_monitoring_access_does_not_reach_the_runtime(corpus, corpus_rows, test_client):
    """The two doors refuse an editor who may not read the dashboard's run
    history (/d checks the release, the draft its conversation); the runtime
    refuses them too."""
    corpus_rows.update("reports", corpus.reports["bow_source"], artifact_visibility="none")
    aid = own(corpus_rows, corpus, "bow_source/dash", owner="bow_scoped", project="private")
    assert test_client.get(f"/api/d/{aid}/draft", headers=as_(corpus, "proj_viewer")).status_code == 403
    assert _runtime(test_client, corpus, aid, "proj_viewer") == 404  # refused like an unknown id (#1304)
    publish(aid)
    assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "proj_viewer")).status_code == 403
    assert _runtime(test_client, corpus, aid, "proj_viewer") == 404  # refused like an unknown id (#1304)
    assert _runtime(test_client, corpus, aid, "bow_scoped") == 200


@pytest.mark.e2e
def test_forks_refusal_for_a_signed_out_caller_is_a_refusal():
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.services import dashboard_lifecycle

    with pytest.raises(HTTPException) as refused:
        asyncio.run(dashboard_lifecycle._fork_eligibility(None, SimpleNamespace(), SimpleNamespace(), None))
    assert refused.value.status_code in (401, 403)


@pytest.mark.e2e
def test_pdf_render_locks_do_not_pile_up():
    """One render of a release at a time -- and the lock goes with its last
    user, so republishing for months does not grow the table (PR #1455
    review)."""
    import asyncio

    from app.routes import dashboard as routes

    async def two_renders():
        async def render():
            async with routes._lock("release-x"):
                await asyncio.sleep(0)
        await asyncio.gather(render(), render())

    asyncio.run(two_renders())
    assert "release-x" not in routes._pdf_locks
