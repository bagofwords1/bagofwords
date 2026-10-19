"""Exports of a published dashboard hold what its reader sees (phase 4, part B).

The rule PR #1304 set for /r (public outputs apply the same viewer rules as
the public view): a /d export of the release embeds, for each chart, exactly
the rows `GET /api/d/{id}/queries/{qid}/step` would give the same reader --
nothing for a chart whose snapshot is one identity's, everything for a plain
one. When the whole snapshot is withheld from a reader (a personal connection
in viewer mode), the reader's export holds their own run's rows -- what they
see on the page (found in manual testing: it used to be refused, as on /r).
A reader with no run of their own yet is told to run it first. A deck stays
refused: it is drawn once, from the owner's rows. The owner exports their own
view.

The browser that renders PDFs and the vendored JS libraries are outside the
app, so they are stubbed; everything that decides the data runs for real.
"""
import json

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share


@pytest.fixture
def no_vendor_libs(monkeypatch):
    """The HTML bundle without the vendored JS (not installed in every test
    environment); the embedded data is the same."""
    from app.services import html_export_service as h

    async def plain(code):
        return code

    monkeypatch.setattr(h, "get_export_vendor_scripts", lambda include_pdf=False: "")
    monkeypatch.setattr(h, "get_globals_script", lambda: "")
    monkeypatch.setattr(h, "get_offline_host_script", lambda: "")
    monkeypatch.setattr(h, "_build_artifact_script", plain)


@pytest.fixture
def pdf_pages(monkeypatch):
    """Capture the HTML each PDF render receives instead of starting Chromium."""
    from app.services.report_pdf_service import ReportPdfService

    seen, written = [], []

    async def render(self, artifact_id, html_content, mode="page"):
        seen.append(html_content)
        out = self.UPLOADS_DIR / f"{artifact_id}.pdf"
        out.write_bytes(b"%PDF-1.4 test")
        written.append(out)
        return f"pdfs/{artifact_id}.pdf"

    monkeypatch.setattr(ReportPdfService, "generate_pdf", render)
    # The page shell inlines the vendored JS too.
    from app.services import report_pdf_service
    monkeypatch.setattr(report_pdf_service, "get_inline_scripts", lambda mode="page": "")
    yield seen
    for path in written:
        path.unlink(missing_ok=True)


def _embedded(html: str) -> dict:
    marker = "window.ARTIFACT_DATA = "
    return json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker):])[0]


def _rows_by_query(payload) -> dict:
    return {v["query_id"]: v["rows"] for v in payload["visualizations"]}


def _published(corpus, corpus_rows, test_client, key):
    aid = own(corpus_rows, corpus, key)
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    return aid, release


def _json_view_rows(test_client, corpus, aid, reader) -> dict:
    out = {}
    for q in test_client.get(f"/api/d/{aid}/queries", headers=as_(corpus, reader)).json():
        step = test_client.get(f"/api/d/{aid}/queries/{q['id']}/step", headers=as_(corpus, reader)).json()
        out[q["id"]] = (step.get("data") or {}).get("rows", [])
    return out


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["html", "pdf"])
@pytest.mark.parametrize("key", ["conv_three_dash/dash_sales", "identity_param/dash"])
def test_an_export_holds_exactly_the_rows_its_reader_sees(
        corpus, corpus_rows, test_client, no_vendor_libs, pdf_pages, fmt, key):
    from app.services.report_pdf_service import ReportPdfService

    aid, release = _published(corpus, corpus_rows, test_client, key)
    for reader in ("member", "owner"):
        resp = test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, reader))
        assert resp.status_code == 200, (reader, resp.text)
        # A reader's PDF holds their rows: it is not kept once sent.
        assert not list(ReportPdfService.UPLOADS_DIR.glob(f"release-{release}-*.pdf"))
        html = resp.text if fmt == "html" else pdf_pages[-1]
        exported = _rows_by_query(_embedded(html))
        seen = _json_view_rows(test_client, corpus, aid, reader)
        assert exported and set(exported) <= set(seen), reader
        assert {q: exported[q] for q in exported} == {q: seen[q] for q in exported}, reader
    if key == "identity_param/dash":
        # The point of the rule: the owner's slice is in the owner's file only.
        member_html = test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, "member"))
        member_rows = _rows_by_query(_embedded(member_html.text if fmt == "html" else pdf_pages[-1]))
        assert all(rows == [] for rows in member_rows.values())


def _copy_step(corpus_rows, release):
    with corpus_rows.engine.connect() as conn:
        return conn.execute(text("SELECT q.default_step_id FROM queries q WHERE q.artifact_release_id = :r"),
                            {"r": release}).scalar_one()


def _own_run(corpus, corpus_rows, release, reader, label):
    # A reader's own run of a personal connection needs the provider; its
    # stored result is what the run leaves behind, so it is written directly.
    rows = [{"מי": label}]
    corpus_rows.insert("step_user_results", step_id=_copy_step(corpus_rows, release),
                       user_id=corpus.cast.id(reader), organization_id=corpus.cast.org_id, report_id=None,
                       status="success", executed_as="viewer", params_fingerprint="",
                       data={"rows": rows, "columns": [{"field": "מי"}]})
    return rows


@pytest.mark.e2e
@pytest.mark.parametrize("fmt", ["html", "pdf"])
def test_a_reader_of_a_personal_dashboard_exports_their_own_rows(
        corpus, corpus_rows, test_client, no_vendor_libs, pdf_pages, fmt):
    aid, release = _published(corpus, corpus_rows, test_client, "user_required/dash")
    mine = _own_run(corpus, corpus_rows, release, "has_results", "has_results")
    resp = test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, "has_results"))
    assert resp.status_code == 200, resp.text
    exported = _rows_by_query(_embedded(resp.text if fmt == "html" else pdf_pages[-1]))
    assert list(exported.values()) == [mine], "the reader's file holds their own run, nothing else"
    # Another reader, with no run of their own, gets neither theirs nor the owner's rows.
    other = test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, "no_creds"))
    assert other.status_code == 409 and other.json()["error_code"] == "dashboard.export_needs_run", other.text
    # The owner exports their own view, as before.
    owner = test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, "owner"))
    assert owner.status_code == 200
    owner_rows = _rows_by_query(_embedded(owner.text if fmt == "html" else pdf_pages[-1]))
    assert any(owner_rows.values()) and list(owner_rows.values()) != [mine]


@pytest.mark.e2e
def test_a_personal_deck_stays_refused_for_readers(corpus, corpus_rows, test_client):
    """A deck is drawn once, at publish, from the owner's rows."""
    aid = own(corpus_rows, corpus, "user_required/dash")
    corpus_rows.update("artifacts", aid, mode="slides")
    release = publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    _own_run(corpus, corpus_rows, release, "has_results", "has_results")
    for fmt in ("pptx", "pdf"):
        refused = test_client.get(f"/api/d/{aid}/export/{fmt}", headers=as_(corpus, "has_results"))
        assert refused.status_code == 409 and refused.json()["error_code"] == "dashboard.export_withheld", fmt


@pytest.mark.e2e
def test_the_page_is_told_whose_rows_an_export_holds(corpus, corpus_rows, test_client):
    aid, _ = _published(corpus, corpus_rows, test_client, "user_required/dash")
    plain, _ = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    meta = lambda ref, who: test_client.get(f"/api/d/{ref}", headers=as_(corpus, who)).json()
    assert meta(aid, "has_results")["exports_own_rows"] is True
    assert meta(aid, "owner")["exports_own_rows"] is False
    assert meta(plain, "member")["exports_own_rows"] is False


@pytest.mark.e2e
def test_the_export_is_the_published_version_not_the_draft(corpus, corpus_rows, test_client, no_vendor_libs):
    aid, release = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
    corpus_rows.update("artifact_versions", corpus.ids["conv_three_dash/dash_sales/v1"],
                       content={"code": "function App() { return 'טיוטה' }", "visualization_ids": []})
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"],
                       data={"rows": [{"חודש": "טיוטה"}], "columns": [{"field": "חודש"}]})
    html = test_client.get(f"/api/d/{aid}/export/html", headers=as_(corpus, "member")).text
    assert "טיוטה" not in html
    with corpus_rows.engine.connect() as conn:
        viz = conn.execute(text("SELECT id FROM visualizations WHERE artifact_release_id = :r"),
                           {"r": release}).scalars().all()
    assert {v["id"] for v in _embedded(html)["visualizations"]} == set(viz)


def _deck_files(corpus_rows, version_id, uploads):
    """A real deck and slide image for a corpus slides version (the corpus
    stores their paths, not the files)."""
    import uuid

    stem = f"corpus-{uuid.uuid4().hex[:8]}"
    deck = uploads / "pptx" / f"{stem}.pptx"
    preview = uploads / "pptx_previews" / stem / "slide-1.png"
    for path, data in ((deck, b"PK\x03\x04 deck"), (preview, b"\x89PNG slide")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return deck, preview


@pytest.mark.e2e
def test_html_export_is_for_pages_and_pptx_for_decks(corpus, corpus_rows, test_client, no_vendor_libs):
    import shutil

    from app.services.report_pdf_service import ReportPdfService

    uploads = ReportPdfService.PPTX_DIR.parent
    deck, preview = _deck_files(corpus_rows, corpus.ids["slides_previews/deck/v1"], uploads)
    corpus_rows.update("artifact_versions", corpus.ids["slides_previews/deck/v1"], pptx_path=str(deck))
    try:
        deck_id, release = _published(corpus, corpus_rows, test_client, "slides_previews/deck")
        resp = test_client.get(f"/api/d/{deck_id}/export/pptx", headers=as_(corpus, "member"))
        assert resp.status_code == 200 and resp.content == b"PK\x03\x04 deck"
        html = test_client.get(f"/api/d/{deck_id}/export/html", headers=as_(corpus, "member"))
        assert html.status_code == 409 and html.json()["error_code"] == "dashboard.export_unavailable"

        corpus_rows.update("artifact_releases", release, pptx_path=str(uploads / "pptx" / "missing.pptx"))
        gone = test_client.get(f"/api/d/{deck_id}/export/pptx", headers=as_(corpus, "member"))
        assert gone.status_code == 409 and gone.json()["error_code"] == "dashboard.export_unavailable"

        page, _ = _published(corpus, corpus_rows, test_client, "conv_three_dash/dash_sales")
        pptx = test_client.get(f"/api/d/{page}/export/pptx", headers=as_(corpus, "member"))
        assert pptx.status_code == 409 and pptx.json()["error_code"] == "dashboard.export_unavailable"
    finally:
        deck.unlink(missing_ok=True)
        shutil.rmtree(preview.parent, ignore_errors=True)


@pytest.mark.e2e
def test_a_deck_holding_one_identitys_rows_is_not_handed_to_other_readers(
        corpus, corpus_rows, test_client, pdf_pages):
    """A deck, its PDF and its slide images bake every chart's stored rows
    in. When one chart is one identity's slice, readers other than the owner
    get none of them; the owner gets all three."""
    import shutil

    from app.services.report_pdf_service import ReportPdfService
    from tests.fixtures.dashboard_corpus.rows import new_id

    uploads = ReportPdfService.PPTX_DIR.parent
    report = corpus.reports["identity_param"]
    aid = corpus_rows.insert("artifacts", id=new_id(), report_id=report, organization_id=corpus.cast.org_id,
                             created_by=corpus.cast.id("owner"), mode="slides", title="מצגת אישית")
    deck, preview = _deck_files(corpus_rows, None, uploads)
    corpus_rows.insert("artifact_versions", artifact_id=aid, report_id=report, user_id=corpus.cast.id("owner"),
                       organization_id=corpus.cast.org_id, version=1, status="completed", pptx_path=str(deck),
                       content={"code": "from pptx import Presentation", "runtime_version": 1,
                                "visualization_ids": [corpus.ids["identity_param/q_sales/viz"]],
                                "preview_images": [f"pptx_previews/{preview.parent.name}/slide-1.png"]})
    corpus_rows.update("artifacts", aid, owner_id=corpus.cast.id("owner"))
    try:
        publish(aid)
        assert share(test_client, corpus, aid, "internal").status_code == 200
        for path in (f"/api/d/{aid}/export/pptx", f"/api/d/{aid}/export/pdf"):
            refused = test_client.get(path, headers=as_(corpus, "member"))
            assert refused.status_code == 409 and refused.json()["error_code"] == "dashboard.export_withheld", path
        assert test_client.get(f"/api/d/{aid}/previews", headers=as_(corpus, "member")).json() == {"count": 0}
        assert test_client.get(f"/api/d/{aid}/previews/0", headers=as_(corpus, "member")).status_code == 404
        assert test_client.get(f"/api/d/{aid}/export/pptx", headers=as_(corpus, "owner")).status_code == 200
        assert test_client.get(f"/api/d/{aid}/previews", headers=as_(corpus, "owner")).json() == {"count": 1}
    finally:
        deck.unlink(missing_ok=True)
        shutil.rmtree(preview.parent, ignore_errors=True)


@pytest.mark.e2e
def test_an_exported_filters_options_are_the_ones_its_reader_sees(corpus, corpus_rows, test_client, no_vendor_libs):
    """A filter's option list comes from another query's rows: when that
    query's snapshot is one identity's, a reader's export lists no options,
    as the step route gives them no rows; the owner's lists them."""
    aid, release = _published(corpus, corpus_rows, test_client, "options_sources/dash")
    with corpus_rows.engine.connect() as conn:
        genres = conn.execute(text("SELECT id FROM queries WHERE artifact_release_id = :r AND title = :t"),
                              {"r": release, "t": "ז'אנרים"}).scalar_one()

    def options(reader):
        html = test_client.get(f"/api/d/{aid}/export/html", headers=as_(corpus, reader))
        assert html.status_code == 200, html.text
        return _embedded(html.text)["params"]["options"].get("genre") or []

    assert options("member") and options("owner")  # plain rows: everyone gets the list
    corpus_rows.update("queries", genres, parameters=[{  # now one identity's slice
        "name": "UserEmail", "type": "string", "source": "identity", "identity_binding": "viewer.email"}])
    step = test_client.get(f"/api/d/{aid}/queries/{genres}/step", headers=as_(corpus, "member")).json()
    assert step["snapshot_withheld"] is True
    assert options("member") == []
    assert options("owner")
