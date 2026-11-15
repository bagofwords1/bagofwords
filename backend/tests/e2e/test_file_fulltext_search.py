"""E2E: the full-text index tier of a file connection (`index_mode=fulltext`).

Drives the real stack — POST /connections (kicks background indexing) →
ConnectionIndexingService → ConnectionService.refresh_schema →
NetworkDirClient.get_schemas → the full-text index → the `search_files` agent
tool — over small file trees written for each test.

What the tier promises, one test per promise:

- any word of any readable file is findable, wherever it is in the file —
  including numbers and IDs, and text far past the start of a long file;
- results are ranked: the file about a subject comes before files that
  mention it, an exact phrase before the same words scattered;
- a hit says where in the file the words are, in `read_file` terms;
- a Hebrew word is found when the file has it with an attached particle;
- the index follows the source: changed files are re-read, unchanged files
  are not, deleted files disappear;
- an interrupted run keeps what it finished;
- a file with no extractable text is reported as such, not silently missing;
- leaving the tier removes the index.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import pytest

from scripts.file_search_corpus import write_docx, write_pdf, write_xlsx

pytestmark = pytest.mark.e2e


# ------------------------------------------------------------------ helpers


def _poll_until_terminal(test_client, connection_id, headers, *, timeout_s: float = 30.0):
    deadline = time.perf_counter() + timeout_s
    last = None
    while time.perf_counter() < deadline:
        r = test_client.get(f"/api/connections/{connection_id}/indexing", headers=headers)
        if r.status_code == 404:
            time.sleep(0.05)
            continue
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("completed", "failed", "cancelled"):
            return last
        time.sleep(0.1)
    pytest.fail(f"Indexing did not reach a terminal state within {timeout_s}s; last={last!r}")


async def _search(conn_id: str, org_id: str, query: str, **kwargs) -> dict:
    """Run the real search_files tool against the connection."""
    from sqlalchemy import select

    from app.ai.tools.implementations.search_files import SearchFilesTool
    from app.dependencies import async_session_maker
    from app.models.organization import Organization

    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).where(Organization.id == str(org_id)))).scalar_one()
        last = None
        async for ev in SearchFilesTool().run_stream(
                {"connection_id": str(conn_id), "query": query, **kwargs},
                {"db": db, "organization": org}):
            last = ev
        return {**last.payload["output"], "observation": last.payload["observation"]}


async def _index_rows(conn_id: str) -> dict:
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.file_index import FileIndexFile

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(FileIndexFile).where(FileIndexFile.connection_id == str(conn_id))
        )).scalars().all()
        return {r.file_id: {"status": r.status, "sections": r.section_count, "version": r.version}
                for r in rows}


def _ids(out: dict) -> list:
    return [f["id"] for f in out["files"]]


@pytest.fixture
def share(create_connection, test_client, create_user, login_user, whoami, tmp_path: Path):
    """A user + org and a helper that connects `tmp_path` as a file share on
    the full-text tier and waits for its first index run."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    class Share:
        root = tmp_path
        headers = None

        def connect(self, index_mode: str = "fulltext", expect: str = "completed", **config):
            conn = create_connection(
                name=f"Share {uuid.uuid4().hex[:6]}", type="network_dir",
                config={"root_path": str(tmp_path), "index_mode": index_mode, **config},
                credentials={}, user_token=token, org_id=org_id,
            )
            self.conn_id = conn["id"]
            run = _poll_until_terminal(test_client, self.conn_id, headers)
            assert run["status"] == expect, run
            return run

        def reindex(self):
            r = test_client.post(f"/api/connections/{self.conn_id}/reindex", headers=headers)
            assert r.status_code == 200, r.text
            run = _poll_until_terminal(test_client, self.conn_id, headers)
            assert run["status"] == "completed", run
            return run

        def set_config(self, **changes):
            r = test_client.get(f"/api/connections/{self.conn_id}", headers=headers)
            assert r.status_code == 200, r.text
            config = {**(r.json().get("config") or {}), **changes}
            r = test_client.put(f"/api/connections/{self.conn_id}", headers=headers,
                                json={"config": config})
            assert r.status_code == 200, r.text

        def search(self, query: str, **kwargs) -> dict:
            return asyncio.run(_search(self.conn_id, org_id, query, **kwargs))

        def index_rows(self) -> dict:
            return asyncio.run(_index_rows(self.conn_id))

    Share.headers = headers
    return Share()


def _filler(words: int = 120) -> str:
    base = ("quarterly budget review for the regional office with supplier payment "
            "terms and delivery schedule approved by the finance manager ").split()
    return " ".join(base[i % len(base)] for i in range(words))


def _touch_later(path: Path, seconds: int = 5) -> None:
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + seconds))


# -------------------------------------------------------------------- tests


def test_any_word_in_a_file_is_findable_including_ids_and_numbers(share):
    (share.root / "orders").mkdir()
    (share.root / "orders" / "batch_a.txt").write_text(
        _filler() + "\nReference PO-48213 was approved for release.\n" + _filler())
    (share.root / "orders" / "batch_b.txt").write_text(
        _filler() + "\nReference PO-77120 is on hold, amount 1930455.\n" + _filler())
    (share.root / "notes.md").write_text("# notes\n" + _filler())
    share.connect()

    out = share.search("PO-48213")
    assert out["index"] == "fulltext" and out["source"] == "catalog"
    assert _ids(out) == ["orders/batch_a.txt"]

    # A bare number that exists only inside a file body.
    assert _ids(share.search("1930455")) == ["orders/batch_b.txt"]
    # A word found nowhere is an answer, not a reason to crawl the source.
    miss = share.search("zzyzxqv")
    assert miss["file_count"] == 0 and miss["source"] == "catalog"


def test_text_far_into_a_long_file_is_indexed_with_its_location(share):
    # The keyword tier samples the first 200k characters; this fact sits
    # after 400k. The hit must point at the byte range that holds it.
    para = _filler(60) + "\n"
    head = para * (400_000 // len(para) + 1)
    needle = "The vorplexian annex governs termination.\n"
    path = share.root / "master_agreement.txt"
    path.write_text(head + needle + para * 20)
    share.connect()

    out = share.search("vorplexian")
    assert _ids(out) == ["master_agreement.txt"]
    match = out["files"][0]["matches"][0]
    assert match["kind"] == "bytes"
    start, length = match["offset"], match["length"]
    assert start <= len(head.encode()) < start + length + len(needle)
    # The location is usable: that byte range of the file holds the word.
    chunk = path.read_bytes()[start:start + length]
    assert b"vorplexian" in chunk
    assert match["read_with"] == {"offset": start, "length": length}


def test_pdf_hits_point_at_the_page_and_workbook_hits_at_the_sheet(share):
    pages = [[f"Page {i} of the services agreement.", _filler(40)] for i in range(1, 13)]
    pages[8].append("Clause on quorblatt escalation applies here.")  # page 9
    write_pdf(share.root / "agreement.pdf", pages)
    write_xlsx(share.root / "costs.xlsx", {
        "Summary": [["center", "budget"], ["north", 100], ["south", 200]],
        "Detail": [["line", "amount"], ["travel", 10]],
        "Overrides": [["code", "reason"], ["CC-7741-GAMMA", "manual override"]],
    })
    share.connect()

    pdf = share.search("quorblatt escalation")
    assert _ids(pdf)[0] == "agreement.pdf"
    m = pdf["files"][0]["matches"][0]
    assert m["kind"] == "page" and m["page"] == 9
    assert m["read_with"] == {"page_range": "9"}

    xlsx = share.search("CC-7741-GAMMA")
    assert _ids(xlsx) == ["costs.xlsx"]
    m = xlsx["files"][0]["matches"][0]
    assert m["kind"] == "sheet" and m["sheet"] == "Overrides"
    # The model-facing observation carries the location too.
    assert "Overrides" in xlsx["observation"]["details"]


def test_results_are_ranked_subject_before_mention_phrase_before_scatter(share):
    # One file ABOUT relocation, many that mention it once.
    about = " ".join(["relocation policy covers relocation allowance and relocation timing"] * 12)
    (share.root / "handbook.txt").write_text(about)
    for i in range(12):
        (share.root / f"update_{i:02d}.txt").write_text(
            _filler(150) + " A short note on relocation follows. " + _filler(150))
    # One file with the exact phrase, others with the same words apart.
    (share.root / "clause_exact.txt").write_text(
        _filler(80) + " The parties agreed an escrow holdback ceiling. " + _filler(80))
    for i in range(4):
        (share.root / f"clause_misc_{i}.txt").write_text(
            "escrow " + _filler(60) + " holdback " + _filler(60) + " ceiling " + _filler(40))
    share.connect()

    assert _ids(share.search("relocation", max_results=5))[0] == "handbook.txt"
    assert _ids(share.search('"escrow holdback ceiling"', max_results=5))[0] == "clause_exact.txt"
    # A file is also findable by the words of its name.
    assert _ids(share.search("handbook"))[0] == "handbook.txt"


def test_hebrew_word_matches_its_form_with_an_attached_particle(share):
    write_docx(share.root / "heskem.docx", [
        "הצדדים הסכימו כי כל מחלוקת תידון בבוררות בלבד.",
        "הודעה תימסר לספק בכתב.",
    ])
    write_docx(share.root / "other.docx", ["מסמך כללי על תקציב המחלקה."])
    share.connect()

    # "בוררות" is written "בבוררות" in the file; "ספק" is written "לספק".
    assert _ids(share.search("בוררות")) == ["heskem.docx"]
    assert _ids(share.search("ספק")) == ["heskem.docx"]
    # And the other way round: the query carries the particle.
    assert "other.docx" in _ids(share.search("בתקציב"))


def test_index_follows_the_source_changed_unchanged_and_deleted_files(share, monkeypatch):
    from app.data_sources.clients.network_dir_client import NetworkDirClient

    a, b, c = (share.root / n for n in ("alpha.txt", "beta.txt", "gamma.txt"))
    a.write_text("alpha file mentions the wimbleton reserve. " + _filler())
    b.write_text("beta file mentions the corvath ledger. " + _filler())
    c.write_text("gamma file mentions the dunmere account. " + _filler())
    share.connect()
    assert _ids(share.search("wimbleton")) == ["alpha.txt"]

    # Count real content reads from here on (the runner is in this process).
    read: list = []
    original = NetworkDirClient._file_sections

    def counting(self, path, name):
        read.append(name)
        return original(self, path, name)

    monkeypatch.setattr(NetworkDirClient, "_file_sections", counting)

    # Unchanged share: nothing is read again.
    share.reindex()
    assert read == []
    assert _ids(share.search("corvath")) == ["beta.txt"]

    # One file changes, one is deleted.
    a.write_text("alpha file now mentions the pellamor reserve. " + _filler())
    _touch_later(a)
    c.unlink()
    run = share.reindex()
    assert read == ["alpha.txt"], read
    assert _ids(share.search("pellamor")) == ["alpha.txt"]
    assert share.search("wimbleton")["file_count"] == 0      # old text is gone
    assert share.search("dunmere")["file_count"] == 0        # deleted file is gone
    assert set(share.index_rows()) == {"alpha.txt", "beta.txt"}
    # The run reports what it did.
    stats = (run.get("stats") or run.get("stats_json") or {}).get("fulltext") or {}
    assert stats.get("written") == 1 and stats.get("reused") == 1 and stats.get("pruned") == 1, run


def test_interrupted_run_keeps_finished_files_and_the_next_run_resumes(share, monkeypatch):
    from app.data_sources.clients.network_dir_client import NetworkDirClient
    from app.data_sources.clients.progress import IndexingCancelled
    from app.services.file_index_store import FileIndexSink

    names = [f"doc_{i:02d}.txt" for i in range(12)]
    for i, n in enumerate(names):
        (share.root / n).write_text(f"document {i} holds marker tok{i:02d}x. " + _filler())

    # Commit after every file so "stopped partway" is exact, then stop the
    # walk at the 8th file, the way a cancel or a timeout does.
    monkeypatch.setattr(FileIndexSink, "BATCH_FILES", 1)
    original = NetworkDirClient._file_sections
    read: list = []

    def stopping(self, path, name):
        read.append(name)
        if len(read) == 8:
            raise IndexingCancelled()
        return original(self, path, name)

    monkeypatch.setattr(NetworkDirClient, "_file_sections", stopping)
    share.connect(expect="cancelled")

    # What the run finished before it stopped is in the index and searchable.
    done = share.index_rows()
    assert len(done) == 7, done
    finished = sorted(done)[0]
    marker = f"tok{int(finished[4:6]):02d}x"
    assert _ids(share.search(marker)) == [finished]

    # The next run reads only the files the first one did not reach.
    read.clear()
    monkeypatch.setattr(
        NetworkDirClient, "_file_sections",
        lambda self, path, name: (read.append(name), original(self, path, name))[1])
    share.reindex()
    assert sorted(read) == sorted(set(names) - set(done)), read
    rows = share.index_rows()
    assert len(rows) == 12 and all(r["status"] == "indexed" for r in rows.values())
    assert _ids(share.search("tok11x")) == ["doc_11.txt"]


def test_file_without_text_is_reported_not_silently_missing(share):
    (share.root / "scan_2021_invoice.pdf").write_bytes(b"%PDF-1.4\n%not a readable pdf\n")
    (share.root / "photo.bin").write_bytes(bytes(range(256)) * 20)
    (share.root / "readme.txt").write_text("plain readable text about onboarding. " + _filler())
    run = share.connect()

    rows = share.index_rows()
    assert rows["readme.txt"]["status"] == "indexed"
    assert rows["scan_2021_invoice.pdf"]["status"] == "no_text"
    assert rows["photo.bin"]["status"] == "no_text"

    # Still findable by name, and flagged in the result.
    out = share.search("invoice 2021")
    assert _ids(out)[0] == "scan_2021_invoice.pdf"
    assert out["files"][0]["index_status"] == "no_text"
    assert out["files"][0]["matches"] == []
    # The model is told some files are not covered.
    assert "no searchable text" in out["observation"]["summary"]
    stats = (run.get("stats") or run.get("stats_json") or {}).get("fulltext") or {}
    assert (stats.get("by_status") or {}).get("no_text") == 2, run


def test_leaving_the_fulltext_tier_removes_the_index(share):
    (share.root / "a.txt").write_text("alpha mentions the hesperine clause. " + _filler())
    share.connect()
    assert share.index_rows()
    assert share.search("hesperine")["index"] == "fulltext"

    share.set_config(index_mode="content")
    share.reindex()
    assert share.index_rows() == {}
    # The keyword tier answers now (or the live scan behind it).
    assert share.search("hesperine")["index"] != "fulltext"


def test_deleting_the_connection_removes_its_index(share, test_client):
    (share.root / "a.txt").write_text("alpha mentions the quillonby clause. " + _filler())
    share.connect()
    assert share.index_rows()

    r = test_client.delete(f"/api/connections/{share.conn_id}", headers=share.headers)
    assert r.status_code in (200, 204), r.text

    async def leftovers():
        from sqlalchemy import func, select

        from app.dependencies import async_session_maker
        from app.models.file_index import FileIndexFile, FileIndexSection

        async with async_session_maker() as db:
            files = (await db.execute(select(func.count()).select_from(FileIndexFile).where(
                FileIndexFile.connection_id == share.conn_id))).scalar_one()
            sections = (await db.execute(select(func.count()).select_from(FileIndexSection).where(
                FileIndexSection.connection_id == share.conn_id))).scalar_one()
            return files, sections

    assert asyncio.run(leftovers()) == (0, 0)


def test_search_is_answered_in_the_database_not_by_loading_the_catalog(share):
    """The keyword tier loads every catalog row of the connection into Python
    and scores it there. The full-text tier must not: its only catalog query
    is for the handful of rows it is about to return."""
    for i in range(300):
        d = share.root / f"dept_{i % 6}"
        d.mkdir(exist_ok=True)
        (d / f"file_{i:04d}.txt").write_text(f"record {i} " + _filler(80))
    (share.root / "dept_3" / "special.txt").write_text("the only mention of kelvanite. " + _filler())
    share.connect(max_catalog_objects=5000)

    async def search_recording_sql():
        from sqlalchemy import event, select

        from app.ai.tools.implementations.search_files import SearchFilesTool
        from app.dependencies import async_session_maker
        from app.models.connection import Connection
        from app.models.organization import Organization

        statements: list = []
        async with async_session_maker() as db:
            conn = (await db.execute(
                select(Connection).where(Connection.id == share.conn_id))).scalar_one()
            org = (await db.execute(
                select(Organization).where(Organization.id == conn.organization_id))).scalar_one()
            engine = db.get_bind().sync_engine if hasattr(db.get_bind(), "sync_engine") else db.get_bind()

            def record(_conn, _cursor, statement, _params, _context, _many):
                statements.append(" ".join(statement.split()).lower())

            event.listen(engine, "before_cursor_execute", record)
            try:
                last = None
                async for ev in SearchFilesTool().run_stream(
                        {"connection_id": share.conn_id, "query": "kelvanite"},
                        {"db": db, "organization": org}):
                    last = ev
            finally:
                event.remove(engine, "before_cursor_execute", record)
        return last.payload["output"], statements

    out, statements = asyncio.run(search_recording_sql())
    assert _ids(out) == ["dept_3/special.txt"]
    catalog_queries = [s for s in statements if "from connection_tables" in s]
    assert catalog_queries, "expected the lookup of the returned rows"
    assert all(" in (" in s for s in catalog_queries), catalog_queries
