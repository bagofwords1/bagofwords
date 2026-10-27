"""Org setting `allow_public_sharing` on first-class dashboards (/d).

Same contract as tests/e2e/rbac/test_public_sharing_policy.py, on the
dashboard's own sharing (Artifact.visibility):
- turned off, a public dashboard opens for its organization only: anonymous
  401, another organization 403, members 200; the stored visibility stays;
- no new public share, on the dashboard or through its conversation's old
  publish/visibility calls; a refused public share does not start a publish;
- a file token handed out on the public page stops working when the link is
  turned off or the dashboard is unshared;
- turning the setting back on restores the same links.
"""
import os
import uuid

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

DASH = "conv_three_dash/dash_sales"


def _set_public_sharing(test_client, corpus, enabled):
    resp = test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                           json={"config": {"allow_public_sharing": {"value": enabled}}})
    assert resp.status_code == 200, resp.text


def _visibility(test_client, corpus, aid):
    return test_client.get(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner")).json()["visibility"]


def _public(corpus, corpus_rows, test_client, key=DASH):
    aid = own(corpus_rows, corpus, key)
    publish(aid)
    assert share(test_client, corpus, aid, "public").status_code == 200
    return aid


def _row(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return dict(conn.execute(text(sql), params).mappings().one())


@pytest.mark.e2e
def test_a_public_dashboard_opens_for_its_organization_only_while_disabled(corpus, corpus_rows, test_client):
    aid = _public(corpus, corpus_rows, test_client)
    page = f"/api/d/{aid}"
    for reader in ("anonymous", "other_org", "member"):
        assert test_client.get(page, headers=as_(corpus, reader)).status_code == 200, reader

    _set_public_sharing(test_client, corpus, False)
    assert test_client.get(page, headers=as_(corpus, "anonymous")).status_code == 401
    assert test_client.get(page, headers=as_(corpus, "other_org")).status_code in (403, 404)
    for reader in ("member", "owner", "admin"):
        assert test_client.get(page, headers=as_(corpus, reader)).status_code == 200, reader
    assert _visibility(test_client, corpus, aid) == "public"  # the policy changed, not the dashboard

    _set_public_sharing(test_client, corpus, True)
    assert test_client.get(page, headers=as_(corpus, "anonymous")).status_code == 200


@pytest.mark.e2e
def test_a_dashboard_cannot_be_made_public_while_disabled(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _set_public_sharing(test_client, corpus, False)

    resp = share(test_client, corpus, aid, "public")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "sharing.public_disabled"
    assert _visibility(test_client, corpus, aid) == "none"
    for visibility in ("internal", "none"):
        assert share(test_client, corpus, aid, visibility).status_code == 200, visibility


@pytest.mark.e2e
def test_an_already_public_dashboard_can_still_be_edited_or_closed(corpus, corpus_rows, test_client):
    aid = _public(corpus, corpus_rows, test_client)
    _set_public_sharing(test_client, corpus, False)
    # Re-sending the unchanged visibility (with an editor added) opens nothing new.
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"),
                           json={"visibility": "public", "editor_user_ids": [corpus.cast.id("member")]})
    assert resp.status_code == 200, resp.text
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert share(test_client, corpus, aid, "public").status_code == 403  # and it can't go back


@pytest.mark.e2e
def test_the_conversations_old_calls_cannot_make_its_dashboard_public(migrated_corpus, corpus_rows, test_client,
                                                                    monkeypatch):
    from app.core import fire_and_forget

    spawned = []
    monkeypatch.setattr(fire_and_forget, "spawn", lambda coro: spawned.append(coro) or coro.close())
    c = migrated_corpus
    _set_public_sharing(test_client, c, False)

    # A published dashboard: the publish toggle, status=published and the old
    # visibility PUT are all refused.
    rid = c.reports["vis_none"]
    dash_sql = "SELECT id, visibility, published_release_id FROM artifacts WHERE report_id = :r AND deleted_at IS NULL"
    for call in (
        lambda: test_client.post(f"/api/reports/{rid}/publish", headers=as_(c, "owner")),
        lambda: test_client.put(f"/api/reports/{rid}", headers=as_(c, "owner"), json={"status": "published"}),
        lambda: test_client.put(f"/api/reports/{rid}/visibility/artifact", headers=as_(c, "owner"),
                                json={"visibility": "public"}),
    ):
        resp = call()
        assert resp.status_code == 403, resp.text
        assert resp.json()["error_code"] == "sharing.public_disabled"
        assert _row(corpus_rows, dash_sql, r=rid)["visibility"] == "none"

    # An unpublished one: the refusal comes before the publish would start.
    rid = c.reports["cron_job_private"]
    before = _row(corpus_rows, dash_sql, r=rid)
    assert before["published_release_id"] is None
    resp = test_client.post(f"/api/reports/{rid}/publish", headers=as_(c, "owner"))
    assert resp.status_code == 403, resp.text
    after = _row(corpus_rows, dash_sql, r=rid)
    assert after["visibility"] == "none" and after["published_release_id"] is None
    assert _row(corpus_rows, "SELECT COUNT(*) AS n FROM artifact_releases WHERE artifact_id = :a",
                a=before["id"])["n"] == 0
    assert not spawned


# ── embedded files on the public page ────────────────────────────────────

@pytest.fixture
def public_file(corpus, corpus_rows, test_client):
    """The embedded-files dashboard, public, with its file's bytes on disk."""
    aid = _public(corpus, corpus_rows, test_client, "embedded_files/dash")
    fid = corpus.ids["embedded_files/file_new"]
    path = _row(corpus_rows, "SELECT path FROM files WHERE id = :f", f=fid)["path"]
    disk = os.path.join(os.getcwd(), "uploads", "files", os.path.basename(path))
    os.makedirs(os.path.dirname(disk), exist_ok=True)
    created = not os.path.exists(disk)
    if created:
        with open(disk, "wb") as fh:
            fh.write(b"\x89PNG " + uuid.uuid4().bytes)
    try:
        yield aid, fid
    finally:
        if created:
            os.remove(disk)


def _mint(test_client, corpus, aid, fid, reader):
    resp = test_client.get(f"/api/d/{aid}/files/{fid}/embed_token", headers=as_(corpus, reader))
    assert resp.status_code == 200, (reader, resp.text)
    return resp.json()["token"]


def _serve(test_client, fid, token):
    return test_client.get(f"/api/files/{fid}/embed", params={"token": token}).status_code


@pytest.mark.e2e
def test_a_file_token_from_the_public_page_stops_working_when_disabled(corpus, test_client, public_file):
    aid, fid = public_file
    tokens = {reader: _mint(test_client, corpus, aid, fid, reader) for reader in ("anonymous", "other_org", "member")}
    for reader, token in tokens.items():
        assert _serve(test_client, fid, token) == 200, reader

    _set_public_sharing(test_client, corpus, False)
    assert _serve(test_client, fid, tokens["anonymous"]) == 403
    assert _serve(test_client, fid, tokens["other_org"]) == 403
    assert _serve(test_client, fid, tokens["member"]) == 200

    _set_public_sharing(test_client, corpus, True)
    assert _serve(test_client, fid, tokens["anonymous"]) == 200


@pytest.mark.e2e
def test_a_file_token_from_the_public_page_stops_working_when_unshared(corpus, test_client, public_file):
    aid, fid = public_file
    token = _mint(test_client, corpus, aid, fid, "anonymous")
    assert _serve(test_client, fid, token) == 200
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert _serve(test_client, fid, token) == 403
