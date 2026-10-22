"""What a reader gets through /d once they are in (phase 4, part B).

Everything comes from the published release: its content, its copied queries
(those the page shows and those its filters and loaded steps need), their
data, embedded files and slide previews. A row of the draft, of another
dashboard or of an older release answers "not found" like a missing one. The
data goes through today's viewer policy, keyed on the dashboard: the owner
sees the snapshot; a reader their own run, the snapshot, or nothing when the
snapshot is one identity's (a personal connection in viewer mode, RLS, an
identity parameter).
"""
import json
import uuid

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share


def _get(test_client, corpus, path, reader="member"):
    return test_client.get(path, headers=as_(corpus, reader))


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _published(corpus, corpus_rows, test_client, key, visibility="internal", **own_kw):
    aid = own(corpus_rows, corpus, key, **own_kw)
    release = publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    return aid, release


def _copies(corpus_rows, release):
    return _rows(corpus_rows, "SELECT q.id, q.title, q.deleted_at, s.status, s.id AS step FROM queries q "
                              "LEFT JOIN steps s ON s.id = q.default_step_id WHERE q.artifact_release_id = :r",
                 r=release)


@pytest.mark.e2e
def test_the_page_gets_the_published_content_not_the_draft(corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    published = _rows(corpus_rows, "SELECT content FROM artifact_releases WHERE id = :r", r=release)[0]["content"]
    body = _get(test_client, corpus, f"/api/d/{aid}/content").json()
    assert body["mode"] == "page"
    assert body["content"] == (published if isinstance(published, dict) else json.loads(published))
    corpus_rows.update("artifact_versions", corpus.ids["conv_three_dash/dash_sales/v1"],
                       content={"code": "function App() { return 'טיוטה חדשה' }", "visualization_ids": []})
    assert _get(test_client, corpus, f"/api/d/{aid}/content").json() == body


@pytest.mark.e2e
def test_queries_are_the_published_copies_including_what_filters_and_loaded_steps_need(
        corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, "refs_in_code/dash")
    listed = _get(test_client, corpus, f"/api/d/{aid}/queries").json()
    copies = _copies(corpus_rows, release)
    servable = {c["id"] for c in copies if c["deleted_at"] is None and c["status"] == "success"}
    assert {q["id"] for q in listed} == servable
    assert {"רשימת אזורים", "נסתרת"} <= {q["title"] for q in listed}  # no chart, but needed
    drafts = {r["id"] for r in _rows(corpus_rows, "SELECT id FROM queries WHERE report_id = :r",
                                     r=corpus.reports["refs_in_code"])}
    assert not drafts & {q["id"] for q in listed}


@pytest.mark.e2e
def test_a_published_copy_serves_its_data_and_code(corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    (copy,) = _copies(corpus_rows, release)
    data = _rows(corpus_rows, "SELECT data, code FROM steps WHERE id = :s", s=copy["step"])[0]
    body = _get(test_client, corpus, f"/api/d/{aid}/queries/{copy['id']}/step").json()
    assert body["snapshot_withheld"] is False and body["id"] == copy["step"]
    assert body["data"]["rows"] and body["code"] == data["code"]


@pytest.mark.e2e
def test_only_the_live_releases_copies_are_served(corpus, corpus_rows, test_client):
    aid, first = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    old_copy = _copies(corpus_rows, first)[0]["id"]
    second = publish(aid)
    new_copy = _copies(corpus_rows, second)[0]["id"]
    other, other_release = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_stock")
    elsewhere = _copies(corpus_rows, other_release)[0]["id"]
    draft = corpus.ids["conv_three_dash/q_sales"]
    assert _get(test_client, corpus, f"/api/d/{aid}/queries/{new_copy}/step").status_code == 200
    for qid in (old_copy, elsewhere, draft, str(uuid.uuid4()), "not-an-id"):
        for path in (f"/api/d/{aid}/queries/{qid}/step", f"/api/d/{aid}/queries/{qid}/export"):
            resp = _get(test_client, corpus, path)
            assert resp.status_code == 404, (qid, path)
            # A malformed id is answered by main's guard before the route, with a
            # plain 404 that names nothing; a well-formed one by the route.
            if qid != "not-an-id":
                assert resp.json()["error_code"] == "artifact.not_found", (qid, path)
    assert {q["id"] for q in _get(test_client, corpus, f"/api/d/{aid}/queries").json()} == {new_copy}


@pytest.mark.e2e
def test_editing_the_draft_changes_nothing_a_reader_sees(corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    (copy,) = _copies(corpus_rows, release)
    resp = _get(test_client, corpus, f"/api/d/{aid}/queries/{copy['id']}/step")
    assert resp.status_code == 200 and resp.json()["data"]["rows"]
    before = resp.json()
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"],
                       data={"rows": [{"חודש": "טיוטה", "הכנסות": 0}], "columns": [{"field": "חודש"}]},
                       code="def generate_df(ds_clients, excel_files):\n    return None\n")
    corpus_rows.update("queries", corpus.ids["conv_three_dash/q_sales"], title="כותרת חדשה בטיוטה")
    assert _get(test_client, corpus, f"/api/d/{aid}/queries/{copy['id']}/step").json() == before
    listed = _get(test_client, corpus, f"/api/d/{aid}/queries")
    assert listed.status_code == 200 and listed.json()
    assert "כותרת חדשה בטיוטה" not in listed.text


@pytest.mark.e2e
def test_a_personal_connection_withholds_the_owners_rows_from_readers(corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, "user_required/dash")
    (copy,) = _copies(corpus_rows, release)
    path = f"/api/d/{aid}/queries/{copy['id']}/step"
    member = _get(test_client, corpus, path).json()
    assert member["snapshot_withheld"] is True and member["data"] == {} and member["code"] == ""
    owner = _get(test_client, corpus, path, "owner").json()
    assert owner["snapshot_withheld"] is False and owner["data"]["rows"]
    assert _get(test_client, corpus, f"/api/d/{aid}/queries/{copy['id']}/export").status_code == 403

    # A reader's own run of the published copy is theirs to see.
    corpus_rows.insert("step_user_results", step_id=copy["step"], user_id=corpus.cast.id("member"),
                       organization_id=corpus.cast.org_id, report_id=None, status="success",
                       executed_as="viewer", params_fingerprint="",
                       data={"rows": [{"שלי": 1}], "columns": [{"field": "שלי"}]})
    mine = _get(test_client, corpus, path).json()
    assert mine["data"]["rows"] == [{"שלי": 1}] and mine["viewer_result"]["status"] == "success"
    # Their run of the draft's step is not.
    assert _get(test_client, corpus, path, "has_results").json()["snapshot_withheld"] is True

    # "Run on my behalf" shares the owner's view on purpose.
    assert test_client.put(f"/api/dashboards/{aid}/settings", headers=as_(corpus, "owner"),
                           json={"shared_run_identity": "creator"}).status_code == 200
    assert _get(test_client, corpus, path, "no_creds").json()["data"]["rows"]


@pytest.mark.e2e
@pytest.mark.parametrize("key", ["rls/dash", "identity_param/dash"])
def test_one_identitys_snapshot_never_reaches_another_reader(corpus, corpus_rows, test_client, key):
    """RLS rows and identity-parameter rows are one person's slice: withheld
    from every other reader, with the identity values redacted."""
    aid, release = _published(corpus, corpus_rows, test_client, key)
    (copy,) = _copies(corpus_rows, release)
    if key == "identity_param/dash":
        corpus_rows.update("steps", copy["step"], applied_params={"UserEmail": "owner@corpus.com"})
    path = f"/api/d/{aid}/queries/{copy['id']}/step"
    body = _get(test_client, corpus, path).json()
    assert body["snapshot_withheld"] is True and body["data"] == {} and body["code"] == ""
    assert "owner@corpus.com" not in str(body)
    assert _get(test_client, corpus, path, "owner").json()["data"]["rows"]


@pytest.mark.e2e
def test_an_embedded_file_opens_only_if_the_published_version_holds_it(corpus, corpus_rows, test_client):
    aid, _ = _published(corpus, corpus_rows, test_client, "embedded_files/dash")
    new, old = corpus.ids["embedded_files/file_new"], corpus.ids["embedded_files/file_old"]
    resp = _get(test_client, corpus, f"/api/d/{aid}/files/{new}/embed_token")
    assert resp.status_code == 200 and resp.json()["file_id"] == new and resp.json()["token"]
    for fid in (old, str(uuid.uuid4()), "not-an-id"):
        assert _get(test_client, corpus, f"/api/d/{aid}/files/{fid}/embed_token").status_code == 404, fid
    assert _get(test_client, corpus, f"/api/d/{aid}/files/{new}/embed_token", "other_org").status_code == 403


@pytest.mark.e2e
def test_slide_previews_come_from_the_release(corpus, corpus_rows, test_client):
    """The deck's slide images, copied as the release's own files. A preview
    path the copy engine refused (outside the previews folder) is not served."""
    import shutil
    from pathlib import Path

    uploads = Path(__file__).resolve().parents[3] / "uploads"
    source = uploads / "pptx_previews" / f"corpus-{uuid.uuid4().hex[:8]}"
    source.mkdir(parents=True, exist_ok=True)
    (source / "slide-1.png").write_bytes(b"\x89PNG slide")
    corpus_rows.update("artifact_versions", corpus.ids["slides_previews/deck/v1"], content={
        "code": "from pptx import Presentation", "runtime_version": 1, "visualization_ids": [],
        # An entry the copy engine refuses keeps its path: "../../main.py"
        # reaches a real file outside the previews. The third resolves inside
        # them, so it is copied like the first.
        "preview_images": [f"pptx_previews/{source.name}/slide-1.png", "../../main.py",
                           f"../pptx_previews/{source.name}/slide-1.png"]})
    release = None
    try:
        aid, release = _published(corpus, corpus_rows, test_client, "slides_previews/deck")
        assert _get(test_client, corpus, f"/api/d/{aid}/previews").json() == {"count": 3}
        image = _get(test_client, corpus, f"/api/d/{aid}/previews/0")
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert image.content == b"\x89PNG slide"
        assert _get(test_client, corpus, f"/api/d/{aid}/previews/2").content == b"\x89PNG slide"
        for index in (1, 3, 99, -1):  # 1: the refused path
            assert _get(test_client, corpus, f"/api/d/{aid}/previews/{index}").status_code == 404, index
    finally:
        shutil.rmtree(source, ignore_errors=True)
        if release:  # only this test's copies: other tests copy in parallel
            shutil.rmtree(uploads / "pptx_previews" / f"release-{release}", ignore_errors=True)


@pytest.mark.e2e
def test_the_data_tab_download_is_what_the_reader_sees(corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales", visibility="public")
    (copy,) = _copies(corpus_rows, release)
    csv = _get(test_client, corpus, f"/api/d/{aid}/queries/{copy['id']}/export?format=csv", "anonymous")
    assert csv.status_code == 200 and "הכנסות" in csv.content.decode("utf-8-sig")
    xlsx = _get(test_client, corpus, f"/api/d/{aid}/queries/{copy['id']}/export?format=xlsx")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    assert _get(test_client, corpus, f"/api/d/{aid}/queries/{copy['id']}/export?format=pdf").status_code == 400


@pytest.mark.e2e
def test_every_data_route_is_closed_to_who_cannot_open_the_dashboard(corpus, corpus_rows, test_client):
    aid, release = _published(corpus, corpus_rows, test_client, "embedded_files/dash", visibility="shared")
    (copy,) = [c for c in _copies(corpus_rows, release) if c["deleted_at"] is None]
    paths = [f"/api/d/{aid}/content", f"/api/d/{aid}/queries", f"/api/d/{aid}/queries/{copy['id']}/step",
             f"/api/d/{aid}/queries/{copy['id']}/export", f"/api/d/{aid}/previews",
             f"/api/d/{aid}/files/{corpus.ids['embedded_files/file_new']}/embed_token",
             f"/api/d/{aid}/export/html", f"/api/d/{aid}/export/pdf", f"/api/d/{aid}/export/pptx"]
    for reader, code in (("member", 403), ("anonymous", 401), ("other_org", 403)):
        for path in paths:
            resp = _get(test_client, corpus, path, reader)
            assert resp.status_code == code, (reader, path, resp.status_code)
            assert "הכנסות" not in resp.text


@pytest.mark.e2e
def test_a_release_without_sources_is_judged_by_the_organizations_like_its_conversation(
        corpus, corpus_rows, test_client):
    """A dashboard from an "Auto" conversation carries no data sources. Its
    code ran against the organization's, so the policy counts those (the ones
    the code names, else all of them): this organization has a personal
    connection, so the snapshot is withheld from readers -- as on /r."""
    aid, release = _published(corpus, corpus_rows, test_client, "auto_conv/dash")
    assert _rows(corpus_rows, "SELECT count(*) AS n FROM artifact_release_data_source_association "
                              "WHERE artifact_release_id = :r", r=release)[0]["n"] == 0
    (copy,) = _copies(corpus_rows, release)
    path = f"/api/d/{aid}/queries/{copy['id']}/step"
    assert _get(test_client, corpus, path).json()["snapshot_withheld"] is True
    assert _get(test_client, corpus, path, "owner").json()["snapshot_withheld"] is False
    rsp = test_client.get(f"/api/r/{corpus.reports['auto_conv']}/queries/"
                          f"{corpus.ids['auto_conv/q_sales']}/step", headers=as_(corpus, "member"))
    assert rsp.json()["snapshot_withheld"] is True  # the same answer the conversation gives


@pytest.mark.e2e
def test_slide_previews_picture_the_owners_data_so_withheld_readers_get_none(corpus, corpus_rows, test_client):
    import shutil
    from pathlib import Path

    uploads = Path(__file__).resolve().parents[3] / "uploads"
    source = uploads / "pptx_previews" / f"corpus-{uuid.uuid4().hex[:8]}"
    source.mkdir(parents=True, exist_ok=True)
    (source / "slide-1.png").write_bytes(b"\x89PNG slide")
    corpus_rows.update("artifact_versions", corpus.ids["slides_previews/deck/v1"], content={
        "code": "from pptx import Presentation", "runtime_version": 1, "visualization_ids": [],
        "preview_images": [f"pptx_previews/{source.name}/slide-1.png"]})
    release = None
    try:
        aid, release = _published(corpus, corpus_rows, test_client, "slides_previews/deck")
        personal = _rows(corpus_rows, "SELECT id FROM data_sources WHERE name = :n", n="מחסן אישי")[0]["id"]
        corpus_rows.insert("artifact_release_data_source_association", artifact_release_id=release,
                           data_source_id=personal)
        assert _get(test_client, corpus, f"/api/d/{aid}/previews").json() == {"count": 0}
        assert _get(test_client, corpus, f"/api/d/{aid}/previews/0").status_code == 404
        assert _get(test_client, corpus, f"/api/d/{aid}/previews", "owner").json() == {"count": 1}
        assert _get(test_client, corpus, f"/api/d/{aid}/previews/0", "owner").status_code == 200
    finally:
        shutil.rmtree(source, ignore_errors=True)
        if release:
            shutil.rmtree(uploads / "pptx_previews" / f"release-{release}", ignore_errors=True)


@pytest.mark.e2e
def test_an_owner_who_left_the_organization_gets_a_readers_view(corpus, corpus_rows, test_client, multi_org):
    """Ownership counts only while the owner is in the organization: on a
    public dashboard they are a reader like anyone, and the personal
    connection's rows are withheld from them too."""
    aid, release = _published(corpus, corpus_rows, test_client, "user_required/dash", visibility="public")
    (copy,) = _copies(corpus_rows, release)
    path = f"/api/d/{aid}/queries/{copy['id']}/step"
    assert _get(test_client, corpus, path, "owner").json()["snapshot_withheld"] is False
    membership = corpus.cast.people["owner"].membership_id
    assert test_client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                              headers=as_(corpus, "admin")).status_code == 204
    assert _get(test_client, corpus, path, "owner").json()["snapshot_withheld"] is True
    assert _get(test_client, corpus, f"/api/d/{aid}/export/html", "owner").status_code == 409
