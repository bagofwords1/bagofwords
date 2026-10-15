"""Email files (.eml / .msg) as first-class documents, with attachments as
sub-files.

The contract under test (see app/data_sources/clients/_email_text.py):

  * an email reads as a flat text: header block, body (text/plain preferred,
    HTML-only stripped), an "Attachments" list carrying a sub-file id per
    attachment, then each attachment's own text — so a term that exists only
    inside an attached PDF indexes and searches on the email;
  * `<email id>#att:<i>:<name>` reads the attachment through read_file like a
    file of its own type (CSV → rows, PDF → text), ONLY via the readable
    parent, with a clear error for a wrong index and a nesting cap;
  * the same holds for every resolution source: a directory connector, a
    SharePoint Server (2010) library, and a conversation upload.

`.eml` fixtures are built with the stdlib; `.msg` fixtures are REAL Outlook
files written by tests/unit/_msg_fixture.py (a minimal CFB + MAPI writer) and
parsed by extract-msg, so the .msg path is exercised end to end, not mocked.
"""
from __future__ import annotations

import io
import os
import uuid
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.tools.implementations._file_tool_common import read_source_bytes
from app.ai.tools.implementations.read_file import ReadFileTool
from app.data_sources.clients._document_text import DOC_EXTS, extract_document_text
from app.data_sources.clients._email_text import (
    EMAIL_MAX_DEPTH,
    render_email_text,
    split_attachment_id,
)
from app.data_sources.clients._keywords import extract_keywords
from app.data_sources.clients.network_dir_client import NetworkDirClient
from tests.unit._msg_fixture import build_msg

# Words chosen so each lives in exactly ONE place: the body, the PDF, the CSV,
# the nested message — a search hit then proves WHICH layer was indexed.
BODY_WORD = "zebrafish"
PDF_WORD = "pangolin"
CSV_WORD = "gadget"
NESTED_WORD = "narwhal"
HEBREW_SUBJECT = "דוח רבעוני"
HEBREW_BODY = "שלום בוב, מצורף הדוח"


def _pdf(text: str) -> bytes:
    """A one-page PDF with real text (pypdf cannot lay out text; write the
    content stream by hand)."""
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject

    w = PdfWriter()
    page = w.add_blank_page(width=300, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)}),
    })
    stream = StreamObject()
    stream._data = f"BT /F1 14 Tf 20 150 Td ({text}) Tj ET".encode()
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _eml(*, subject: str = "Quarterly numbers", body: str = f"Attached is the {BODY_WORD} report.",
         html: str | None = None, attachments=(), nested: EmailMessage | None = None,
         charset: str = "utf-8") -> bytes:
    m = EmailMessage(policy=policy.default)
    m["From"] = "Alice <alice@example.com>"
    m["To"] = "Bob <bob@example.com>"
    m["Cc"] = "Carol <carol@example.com>"
    m["Date"] = "Mon, 05 Jan 2026 10:00:00 +0200"
    m["Subject"] = subject
    if html is not None and body is None:
        m.set_content(html, subtype="html", charset=charset)
    else:
        m.set_content(body, charset=charset)
        if html is not None:
            m.add_alternative(html, subtype="html")
    for name, data, mime in attachments:
        main, _, sub = mime.partition("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    if nested is not None:
        m.add_attachment(nested, filename="fwd.eml")
    return m.as_bytes()


STANDARD_ATTACHMENTS = (
    ("report.pdf", _pdf(f"{PDF_WORD} budget figures"), "application/pdf"),
    ("data.csv", f"item,amount\nwidget,10\n{CSV_WORD},32\n".encode(), "text/csv"),
)


def _nested_eml() -> EmailMessage:
    inner = EmailMessage(policy=policy.default)
    inner["From"] = "Dana <dana@example.com>"
    inner["To"] = "Alice <alice@example.com>"
    inner["Subject"] = "FW: original thread"
    inner.set_content(f"The {NESTED_WORD} figures are attached.")
    inner.add_attachment(b"a,b\n1,2\n3,4\n", maintype="text", subtype="csv", filename="inner.csv")
    return inner


def _msg(**kw) -> bytes:
    spec = {
        "subject": "Quarterly numbers", "sender_name": "Alice", "sender_email": "alice@example.com",
        "to": [("Bob", "bob@example.com")], "cc": [("Carol", "carol@example.com")],
        "body": f"Attached is the {BODY_WORD} report.",
        "attachments": [{"name": n, "data": d, "mime": m} for n, d, m in STANDARD_ATTACHMENTS],
    }
    spec.update(kw)
    return build_msg(**spec)


def _with_attachments(ext: str) -> bytes:
    return _eml(attachments=STANDARD_ATTACHMENTS) if ext == "eml" else _msg()


# ------------------------------------------------------------ rendering


class TestRendering:
    @pytest.mark.parametrize("ext", ["eml", "msg"])
    def test_headers_body_and_attachment_list(self, ext):
        text = render_email_text(_with_attachments(ext), f"mail.{ext}", 200_000)
        for needle in ("alice@example.com", "bob@example.com", "carol@example.com",
                       "Quarterly numbers", BODY_WORD):
            assert needle in text
        # Every attachment is listed with a sub-file id the model can read.
        assert "#att:0:report.pdf" in text
        assert "#att:1:data.csv" in text
        # Header block precedes body precedes the attachment list.
        assert text.index("Subject:") < text.index(BODY_WORD) < text.index("#att:0:")

    def test_html_only_body_is_stripped_to_text(self):
        raw = _eml(body=None, html=f"<html><body><p>Hello <b>Bob</b>,</p><p>see the {BODY_WORD} chart.</p></body></html>")
        text = render_email_text(raw, "m.eml", 200_000)
        assert BODY_WORD in text and "Hello Bob" in text
        assert "<p>" not in text and "<b>" not in text

    def test_plain_part_preferred_over_html(self):
        raw = _eml(body="plain wins", html="<p>html loses</p>")
        text = render_email_text(raw, "m.eml", 200_000)
        assert "plain wins" in text and "html loses" not in text

    @pytest.mark.parametrize("ext", ["eml", "msg"])
    def test_hebrew_headers_and_body_survive(self, ext):
        if ext == "eml":
            raw = _eml(subject=HEBREW_SUBJECT, body=HEBREW_BODY, charset="windows-1255")
            assert b"=?" in raw  # the subject really was RFC 2047-encoded
        else:
            raw = _msg(subject=HEBREW_SUBJECT, body=HEBREW_BODY)
        text = render_email_text(raw, f"m.{ext}", 200_000)
        assert HEBREW_SUBJECT in text and HEBREW_BODY in text

    @pytest.mark.parametrize("ext", ["eml", "msg"])
    def test_attachment_text_is_part_of_the_extraction(self, ext):
        text = render_email_text(_with_attachments(ext), f"mail.{ext}", 200_000)
        assert PDF_WORD in text and CSV_WORD in text

    def test_nested_message_rendered_inline_with_chained_ids(self):
        raw = _eml(attachments=STANDARD_ATTACHMENTS, nested=_nested_eml())
        text = render_email_text(raw, "mail.eml", 200_000)
        assert NESTED_WORD in text and "dana@example.com" in text
        assert "#att:2:fwd.eml#att:0:inner.csv" in text

    def test_nested_msg_inside_msg(self):
        raw = _msg(attachments=[{"name": "fwd.msg", "message": {
            "subject": "FW: inner", "sender_name": "Dana", "sender_email": "dana@example.com",
            "to": [("Alice", "alice@example.com")], "body": f"inner {NESTED_WORD} body",
            "attachments": [{"name": "notes.txt", "data": b"inner notes", "mime": "text/plain"}],
        }}])
        text = render_email_text(raw, "mail.msg", 200_000)
        assert NESTED_WORD in text and "inner notes" in text
        parent, chain = split_attachment_id(text.split("file_id: ")[-1].split("\n")[0])
        assert len(chain) == 2 and chain[1][1] == "notes.txt"

    def test_depth_cap_stops_expanding(self):
        # 0: outer → 1: fwd → 2: fwd → 3: fwd (must not be expanded).
        level3 = EmailMessage(policy=policy.default)
        level3["Subject"] = "level three"
        level3.set_content("LEVELTHREEBODY")
        level2 = EmailMessage(policy=policy.default)
        level2["Subject"] = "level two"
        level2.set_content("LEVELTWOBODY")
        level2.add_attachment(level3, filename="l3.eml")
        level1 = EmailMessage(policy=policy.default)
        level1["Subject"] = "level one"
        level1.set_content("LEVELONEBODY")
        level1.add_attachment(level2, filename="l2.eml")
        raw = _eml(nested=level1)
        text = render_email_text(raw, "mail.eml", 200_000)
        assert "LEVELONEBODY" in text and "LEVELTWOBODY" in text
        assert "LEVELTHREEBODY" not in text
        assert EMAIL_MAX_DEPTH == 3

    def test_bounded_by_max_chars_and_attachment_list_survives(self):
        raw = _eml(body="x" * 50_000, attachments=STANDARD_ATTACHMENTS)
        text = render_email_text(raw, "mail.eml", 6_000)
        assert len(text) <= 6_000
        assert "#att:0:report.pdf" in text

    @pytest.mark.parametrize("ext", ["eml", "msg"])
    def test_garbage_never_raises(self, ext, tmp_path: Path):
        p = tmp_path / f"bad.{ext}"
        p.write_bytes(os.urandom(2048))
        # Random bytes are not an Outlook file; the RFC 822 parser, by design,
        # accepts anything as a headerless message. Either way: no exception,
        # a bounded string, and the connector's read completes.
        text = render_email_text(p.read_bytes(), p.name, 1000)
        assert isinstance(text, str) and len(text) <= 1000
        if ext == "msg":
            assert text == "" and extract_document_text(str(p), p.name) == ""
        payload = NetworkDirClient(root_path=str(tmp_path)).read_file(p.name)
        assert isinstance(payload, (str, bytes, bytearray))

    def test_email_extensions_are_documents(self):
        assert {"eml", "msg"} <= DOC_EXTS


# ------------------------------------------------------- id grammar


class TestAttachmentIds:
    def test_split_round_trip(self):
        assert split_attachment_id("inbox/mail.eml") == ("inbox/mail.eml", [])
        assert split_attachment_id("inbox/mail.eml#att:0:report.pdf") == ("inbox/mail.eml", [(0, "report.pdf")])
        assert split_attachment_id("a.msg#att:2:FW: x.eml#att:0:n.txt") == ("a.msg", [(2, "FW: x.eml"), (0, "n.txt")])
        # A '#' that is not the attachment grammar is part of the name.
        assert split_attachment_id("notes#1.txt") == ("notes#1.txt", [])


# ------------------------------------------------ indexing & search


class TestIndexing:
    @pytest.mark.parametrize("ext", ["eml", "msg"])
    def test_keywords_cover_body_and_attachments(self, ext):
        text = render_email_text(_with_attachments(ext), f"mail.{ext}", 200_000)
        kws = extract_keywords(text, f"mail.{ext}")
        assert {BODY_WORD, PDF_WORD, CSV_WORD} <= set(kws)

    @pytest.mark.parametrize("ext", ["eml", "msg"])
    def test_network_dir_content_index_and_live_search(self, ext, tmp_path: Path):
        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / f"mail.{ext}").write_bytes(_with_attachments(ext))
        (tmp_path / "other.txt").write_text("unrelated\n")
        client = NetworkDirClient(root_path=str(tmp_path), index_mode="content")
        rows = {t.name: t.metadata_json["network_dir"] for t in client.get_schemas()}
        meta = rows[f"inbox/mail.{ext}"]
        assert meta["indexed"] is True
        assert {BODY_WORD, PDF_WORD} <= set(meta["keywords"])
        # Live (deep) search finds the email by a term that only the
        # attached PDF contains.
        hits = [f["id"] for f in client.search_files(PDF_WORD)]
        assert hits == [f"inbox/mail.{ext}"]
        # grep sees the same text.
        grep = client.grep_files(PDF_WORD, is_regex=False)
        assert [m["file_id"] for m in grep["matches"]] == [f"inbox/mail.{ext}"]

    def test_msg_listing_reports_a_mime_type(self, tmp_path: Path):
        (tmp_path / "x.msg").write_bytes(_msg())
        (tmp_path / "y.eml").write_bytes(_eml())
        mimes = {f["name"]: f["mime_type"] for f in NetworkDirClient(root_path=str(tmp_path)).list_files()}
        assert mimes["x.msg"] and "outlook" in mimes["x.msg"]
        assert mimes["y.eml"] == "message/rfc822"


# ------------------------------------------------ read_file tool


async def _read(client, file_id: str, runtime_ctx=None, **inputs) -> dict:
    """Run the real read_file tool against a client (no DB)."""
    target = "app.ai.tools.implementations.read_file.resolve_file_client"
    with patch(target, new=AsyncMock(return_value=(client, None))):
        events = [e async for e in ReadFileTool().run_stream(
            {"connection_id": "C1", "file_id": file_id, **inputs}, runtime_ctx or {})]
    return events[-1].payload


def _dir_client(tmp_path: Path, ext: str, **kw) -> NetworkDirClient:
    (tmp_path / "inbox").mkdir(exist_ok=True)
    raw = _eml(attachments=STANDARD_ATTACHMENTS, nested=_nested_eml()) if ext == "eml" else _msg()
    (tmp_path / "inbox" / f"mail.{ext}").write_bytes(raw)
    return NetworkDirClient(root_path=str(tmp_path), **kw)


class TestReadFileTool:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ext", ["eml", "msg"])
    async def test_email_reads_as_text_with_absolute_attachment_ids(self, tmp_path, ext):
        payload = await _read(_dir_client(tmp_path, ext), f"inbox/mail.{ext}")
        out = payload["output"]
        assert out["success"] and out["content_type"] == "text"
        assert f"inbox/mail.{ext}#att:0:report.pdf" in out["text"]
        assert "<this-email>" not in out["text"]
        assert BODY_WORD in out["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ext", ["eml", "msg"])
    async def test_csv_attachment_reads_as_rows(self, tmp_path, ext):
        payload = await _read(_dir_client(tmp_path, ext), f"inbox/mail.{ext}#att:1:data.csv")
        out = payload["output"]
        assert out["success"], out
        assert out["content_type"] == "tabular"
        assert out["row_count"] == 2 and CSV_WORD in out["csv"]
        assert out["file_name"].endswith("data.csv")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ext", ["eml", "msg"])
    async def test_pdf_attachment_reads_as_text(self, tmp_path, ext):
        payload = await _read(_dir_client(tmp_path, ext), f"inbox/mail.{ext}#att:0:report.pdf")
        out = payload["output"]
        assert out["success"], out
        assert out["content_type"] == "text" and PDF_WORD in out["text"]
        assert out["file_name"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_nested_chain_reads_inner_attachment(self, tmp_path):
        client = _dir_client(tmp_path, "eml")
        payload = await _read(client, "inbox/mail.eml#att:2:fwd.eml#att:0:inner.csv")
        out = payload["output"]
        assert out["success"], out
        assert out["content_type"] == "tabular" and out["row_count"] == 2
        # The nested message itself reads as an email.
        payload = await _read(client, "inbox/mail.eml#att:2:fwd.eml")
        out = payload["output"]
        assert out["success"] and NESTED_WORD in out["text"]
        assert "inbox/mail.eml#att:2:fwd.eml#att:0:inner.csv" in out["text"]

    @pytest.mark.asyncio
    async def test_unknown_index_and_stale_name_are_clear_errors(self, tmp_path):
        client = _dir_client(tmp_path, "eml")
        out = (await _read(client, "inbox/mail.eml#att:9:nope.pdf"))["output"]
        assert out["success"] is False
        assert "#9" in out["error"] and "report.pdf" in out["error"]
        out = (await _read(client, "inbox/mail.eml#att:0:renamed.pdf"))["output"]
        assert out["success"] is False and "report.pdf" in out["error"]
        out = (await _read(client, "inbox/mail.eml#att:1:data.csv#att:0:x"))["output"]
        assert out["success"] is False and "not an email" in out["error"]

    @pytest.mark.asyncio
    async def test_depth_cap_on_ids(self, tmp_path):
        client = _dir_client(tmp_path, "eml")
        too_deep = "inbox/mail.eml" + "#att:0:a.eml" * (EMAIL_MAX_DEPTH + 1)
        out = (await _read(client, too_deep))["output"]
        assert out["success"] is False and str(EMAIL_MAX_DEPTH) in out["error"]

    @pytest.mark.asyncio
    async def test_attachment_unreachable_when_parent_is_out_of_scope(self, tmp_path):
        # Scope that excludes the email: the attachment inherits the denial.
        client = _dir_client(tmp_path, "eml", include_globs="*.txt")
        out = (await _read(client, "inbox/mail.eml#att:1:data.csv"))["output"]
        assert out["success"] is False
        # The denial names the PARENT (the thing that is out of scope) and
        # leaks nothing about the attachment's content.
        assert "inbox/mail.eml" in out["error"] and CSV_WORD not in out["error"]
        assert "denied" in out["error"].lower() or "outside" in out["error"].lower()

    @pytest.mark.asyncio
    async def test_windowed_and_page_reads_work_on_attachments(self, tmp_path):
        client = _dir_client(tmp_path, "eml")
        out = (await _read(client, "inbox/mail.eml#att:1:data.csv", offset=0, length=12))["output"]
        assert out["success"] and out["windowed"] and out["text"].startswith("item")
        out = (await _read(client, "inbox/mail.eml#att:0:report.pdf", page_range="1"))["output"]
        assert out["success"] and PDF_WORD in out["text"] and out["pages_total"] == 1

    @pytest.mark.asyncio
    async def test_read_source_bytes_serves_attachments_for_attach_file(self, tmp_path):
        client = _dir_client(tmp_path, "eml")
        data, name, mime = await read_source_bytes(client, "inbox/mail.eml#att:1:data.csv")
        assert name == "data.csv" and mime == "text/csv" and CSV_WORD.encode() in data
        data, name, _ = await read_source_bytes(client, "inbox/mail.eml#att:2:fwd.eml#att:0:inner.csv")
        assert name == "inner.csv" and data.startswith(b"a,b")


# ------------------------------------------------- conversation uploads


class TestChatUpload:
    """An .eml uploaded to the conversation resolves through the session-file
    ladder (no connection_id) and its attachments through the same ids."""

    def _ctx(self, tmp_path: Path, ext: str):
        fid = str(uuid.uuid4())
        path = tmp_path / f"{fid}_mail.{ext}"
        path.write_bytes(_with_attachments(ext))
        f = MagicMock()
        f.id, f.path, f.filename, f.organization_id = fid, str(path), f"mail.{ext}", None
        report = MagicMock()
        report.files = [f]
        return fid, {"report": report}

    async def _read_session(self, file_id: str, ctx: dict) -> dict:
        events = [e async for e in ReadFileTool().run_stream({"file_id": file_id}, ctx)]
        return events[-1].payload["output"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ext", ["eml", "msg"])
    async def test_uploaded_email_and_its_attachments(self, tmp_path, ext):
        fid, ctx = self._ctx(tmp_path, ext)
        out = await self._read_session(fid, ctx)
        assert out["success"] and out["content_type"] == "text"
        assert BODY_WORD in out["text"] and f"{fid}#att:1:data.csv" in out["text"]
        out = await self._read_session(f"{fid}#att:1:data.csv", ctx)
        assert out["success"], out
        assert out["content_type"] == "tabular" and out["row_count"] == 2
        out = await self._read_session(f"{fid}#att:0:report.pdf", ctx)
        assert out["success"] and PDF_WORD in out["text"]

    @pytest.mark.asyncio
    async def test_foreign_session_id_stays_unresolved(self, tmp_path):
        _fid, ctx = self._ctx(tmp_path, "eml")
        out = await self._read_session(f"{uuid.uuid4()}#att:0:report.pdf", ctx)
        assert out["success"] is False

    @pytest.mark.asyncio
    async def test_upload_under_the_app_directory_is_readable(self):
        """Uploads are stored in uploads/files INSIDE the backend directory —
        the directory the admin-facing network_dir root policy refuses. The
        session-file read path is the app's own, so it must not be subject
        to that policy (found by the sandbox loop: every chat-upload read
        failed with 'protected system or application directory')."""
        import shutil

        base = Path("uploads") / "files" / f"test-{uuid.uuid4().hex}"
        base.mkdir(parents=True)
        try:
            fid, ctx = self._ctx(base, "eml")
            out = await self._read_session(fid, ctx)
            assert out["success"], out
            assert BODY_WORD in out["text"]
            out = await self._read_session(f"{fid}#att:1:data.csv", ctx)
            assert out["success"] and out["content_type"] == "tabular"
        finally:
            shutil.rmtree(base, ignore_errors=True)
