"""Feedback loop — unified read_file/grep_files over the report space
(session files), images to vision, and page_range × vision for scanned PDFs.

Today read_file/grep_files resolve ONLY file-source connections
(`resolve_file_client` walks report.data_sources). Files attached to the
report itself (uploads, attach_file results) have NO reader at all — and an
image from an earlier turn is architecturally unreachable (eager vision only
covers the current completion). These tests assert the unified contract and
FAIL on main by design.
"""
from __future__ import annotations

import io
import json
import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.test_pdf_surrogate_sanitization import build_multipage_pdf


# ------------------------------------------------------------------ fixtures


def _mk_file(tmp_path, name: str, data: bytes, content_type: str, org="ORG-1"):
    """A File-model-shaped object backed by a real file on disk."""
    path = tmp_path / f"{_uuid.uuid4()}_{name}"
    path.write_bytes(data)
    f = MagicMock()
    f.id = str(_uuid.uuid4())
    f.filename = name
    f.path = str(path)
    f.content_type = content_type
    f.organization_id = org
    return f


def _runtime_ctx(files, org_id="ORG-1", supports_vision=True):
    report = MagicMock()
    report.id = "REP-1"
    report.files = files
    report.data_sources = []
    org = MagicMock()
    org.id = org_id
    org.settings = None
    model = MagicMock()
    model.supports_vision = supports_vision
    return {"report": report, "organization": org, "model": model}


def _png_bytes(width=64, height=32, color=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _textless_pdf(page_sizes) -> bytes:
    """PDF with no text ops; each page gets a distinct MediaBox size so the
    rendered PNG dimensions identify WHICH page was rasterized."""
    objs = []
    n = len(page_sizes)
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))

    def obj(num, body):
        return f"{num} 0 obj\n".encode() + body + b"\nendobj\n"

    objs.append(obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append(obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()))
    for i, (w, h) in enumerate(page_sizes):
        objs.append(obj(3 + i, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] >>".encode()))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for o in objs:
        offsets.append(out.tell())
        out.write(o)
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


async def _run_read(tool_input, runtime_ctx):
    from app.ai.tools.implementations.read_file import ReadFileTool
    events = [e async for e in ReadFileTool().run_stream(tool_input, runtime_ctx)]
    return events[-1].payload


async def _run_grep(tool_input, runtime_ctx):
    from app.ai.tools.implementations.grep_files import GrepFilesTool
    events = [e async for e in GrepFilesTool().run_stream(tool_input, runtime_ctx)]
    return events[-1].payload


# ---------------------------------------------------- resolution & access


class TestSessionResolution:
    @pytest.mark.asyncio
    async def test_session_json_read_without_connection(self, tmp_path):
        body = json.dumps({"verification_code": "CODE-SESSION-11AA", "n": list(range(50))})
        f = _mk_file(tmp_path, "rules.json", body.encode(), "application/json")
        payload = await _run_read({"connection_id": "", "file_id": f.id}, _runtime_ctx([f]))
        assert payload["output"]["success"] is True, payload["output"].get("error")
        assert "CODE-SESSION-11AA" in (payload["observation"].get("details") or "")

    @pytest.mark.asyncio
    async def test_other_reports_file_is_not_readable(self, tmp_path):
        mine = _mk_file(tmp_path, "a.txt", b"mine", "text/plain")
        foreign = _mk_file(tmp_path, "b.txt", b"secret", "text/plain")
        payload = await _run_read(
            {"connection_id": "", "file_id": foreign.id}, _runtime_ctx([mine])
        )
        assert payload["output"]["success"] is False
        assert "secret" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_org_mismatch_denied(self, tmp_path):
        f = _mk_file(tmp_path, "a.txt", b"data", "text/plain", org="OTHER-ORG")
        payload = await _run_read({"connection_id": "", "file_id": f.id}, _runtime_ctx([f]))
        assert payload["output"]["success"] is False

    @pytest.mark.asyncio
    async def test_explicit_connection_id_still_routes_to_connector(self, tmp_path):
        """Explicit beats implicit: a connection_id must NOT silently fall back
        to the session space."""
        f = _mk_file(tmp_path, "a.txt", b"session data", "text/plain")
        ctx = _runtime_ctx([f])
        with patch(
            "app.ai.tools.implementations.read_file.resolve_file_client",
            new=AsyncMock(return_value=(None, "no such connection")),
        ):
            payload = await _run_read({"connection_id": "CONN-X", "file_id": f.id}, ctx)
        assert payload["output"]["success"] is False
        assert "session data" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_windowed_read_over_session_file(self, tmp_path):
        body = "".join(f"line-{i} MARKER-{i}\n" for i in range(2000))
        f = _mk_file(tmp_path, "big.log", body.encode(), "text/plain")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id, "offset": 0, "length": 500},
            _runtime_ctx([f]),
        )
        out = payload["output"]
        assert out["success"] is True and out["windowed"] is True
        assert out["next_cursor"] and not out["eof"]
        assert "MARKER-0" in (payload["observation"].get("details") or "")

    @pytest.mark.asyncio
    async def test_page_range_over_session_pdf(self, tmp_path):
        pdf = build_multipage_pdf([
            "Alpha page one with introduction text UNIQUE-A",
            "Bravo page two carries the verification code UNIQUE-B",
            "Charlie page three appendix and closing notes UNIQUE-C",
        ])
        f = _mk_file(tmp_path, "book.pdf", pdf, "application/pdf")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id, "page_range": "2"}, _runtime_ctx([f])
        )
        out = payload["output"]
        assert out["success"] is True
        assert out["pages_shown"] == "2-2" and out["pages_total"] == 3
        assert "UNIQUE-B" in (payload["observation"].get("details") or "")

    @pytest.mark.asyncio
    async def test_session_read_does_not_reattach(self, tmp_path):
        f = _mk_file(tmp_path, "a.txt", b"hello world", "text/plain")
        ctx = _runtime_ctx([f])
        with patch(
            "app.ai.tools.implementations.read_file.attach_drive_file_to_session",
            new=AsyncMock(return_value="NEW-ID"),
        ) as attach:
            payload = await _run_read({"connection_id": "", "file_id": f.id}, ctx)
        assert payload["output"]["success"] is True
        assert not attach.called  # already in the space — no duplicate rows
        assert payload["output"].get("session_file_id") == f.id


# ------------------------------------------------------------------ images


class TestImageReads:
    @pytest.mark.asyncio
    async def test_session_png_renders_to_vision_blocks(self, tmp_path):
        f = _mk_file(tmp_path, "shot.png", _png_bytes(64, 32), "image/png")
        payload = await _run_read({"connection_id": "", "file_id": f.id}, _runtime_ctx([f]))
        out = payload["output"]
        assert out["success"] is True
        assert out["content_type"] == "images"
        imgs = payload["observation"].get("images") or []
        assert len(imgs) == 1 and imgs[0]["media_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_no_vision_model_degrades_gracefully(self, tmp_path):
        f = _mk_file(tmp_path, "shot.png", _png_bytes(), "image/png")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id},
            _runtime_ctx([f], supports_vision=False),
        )
        assert payload["output"]["success"] is True
        assert not (payload["observation"].get("images") or [])

    @pytest.mark.asyncio
    async def test_scanned_pdf_page_range_renders_that_page(self, tmp_path):
        pdf = _textless_pdf([(100, 100), (300, 150), (100, 100)])
        f = _mk_file(tmp_path, "scan.pdf", pdf, "application/pdf")
        payload = await _run_read(
            {"connection_id": "", "file_id": f.id, "page_range": "2"}, _runtime_ctx([f])
        )
        out = payload["output"]
        assert out["success"] is True, out.get("error")
        imgs = payload["observation"].get("images") or []
        assert len(imgs) == 1
        import base64
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(imgs[0]["data"])))
        # Page 2 is 300x150 — a 2:1 aspect no other page has.
        assert abs(im.width / im.height - 2.0) < 0.05
        assert out["pages_total"] == 3


# ------------------------------------------------------------- grep_files


class TestSessionGrep:
    @pytest.mark.asyncio
    async def test_grep_sweeps_session_files(self, tmp_path):
        files = [
            _mk_file(tmp_path, "a.log", b"ok\nERR_X hit here\nok\n", "text/plain"),
            _mk_file(tmp_path, "b.json", b'{"msg": "ERR_X in json"}\n', "application/json"),
            _mk_file(tmp_path, "img.png", _png_bytes(), "image/png"),
        ]
        payload = await _run_grep(
            {"connection_id": "", "pattern": "ERR_X", "is_regex": False},
            _runtime_ctx(files),
        )
        out = payload["output"]
        assert out["success"] is True, out.get("error")
        hit_files = {m["path"] for m in out["matches"]}
        assert hit_files == {"a.log", "b.json"}
        # The image was sniffed as binary and reported, not silently dropped.
        assert any(s["reason"] == "binary" for s in out["files_skipped"])
        assert "ERR_X hit here" in (payload["observation"].get("details") or "")


# --------------------------------------------------------------- previews


class TestFirstTurnPreviews:
    """The <files> index must give the planner enough to DECIDE to read —
    today json/text/images render as 'unsupported' (name-only)."""

    def test_json_gets_head_preview(self, tmp_path):
        from app.services.file_preview import generate_file_preview, render_file_description
        f = _mk_file(tmp_path, "rules.json", b'{"policy": "pricing", "x": 1}', "application/json")
        p = generate_file_preview(f)
        assert p["type"] == "text" and '"policy"' in p["head"]
        desc = render_file_description(p, f.path)
        assert "pricing" in desc

    def test_big_text_head_points_to_read_file(self, tmp_path):
        from app.services.file_preview import generate_file_preview, render_file_description
        f = _mk_file(tmp_path, "app.log", b"x" * 5000, "text/plain")
        p = generate_file_preview(f)
        assert p["head_truncated"] is True
        assert "read_file" in render_file_description(p, f.path)

    def test_image_preview_has_dimensions(self, tmp_path):
        from app.services.file_preview import generate_file_preview, render_file_description
        f = _mk_file(tmp_path, "shot.png", _png_bytes(120, 80), "image/png")
        p = generate_file_preview(f)
        assert p["type"] == "image" and p["width"] == 120 and p["height"] == 80
        assert "read_file" in render_file_description(p, f.path)

    def test_pdf_preview_advertises_page_range(self, tmp_path):
        from app.services.file_preview import generate_file_preview, render_file_description
        pages = [f"Page {i} content with enough text to matter" for i in range(1, 8)]
        f = _mk_file(tmp_path, "big.pdf", build_multipage_pdf(pages), "application/pdf")
        p = generate_file_preview(f)
        desc = render_file_description(p, f.path)
        assert "Total pages: 7" in desc
        assert "page_range" in desc


# ---------------------------------------------------------- history digest


class TestHistoryDigest:
    def _te(self, rj):
        te = MagicMock()
        te.tool_name = "read_file"
        te.result_json = rj
        return te

    def test_digest_records_pages_read(self):
        from app.ai.context.builders.message_context_builder import _digest_file_tool
        d = _digest_file_tool(self._te({
            "file_id": "F1", "content_type": "text",
            "pages_shown": "2-2", "pages_total": 38, "text": "x",
        }))
        assert "pages 2-2 of 38" in d

    def test_digest_records_image_view_and_reaccess_hint(self):
        from app.ai.context.builders.message_context_builder import _digest_file_tool
        d = _digest_file_tool(self._te({
            "file_id": "F2", "content_type": "images", "image_count": 1,
            "image_file_ids": ["F2"],
        }))
        assert "viewed by vision" in d and "read_file" in d


# ---------------------------------------------------------- catalog gating


class TestCatalogGating:
    def test_read_and_grep_available_when_report_has_files(self):
        from app.ai.registry import ToolRegistry

        reg = ToolRegistry()
        # Simulates agent_v2's capability derivation for a report with
        # uploaded files and NO file connector.
        from app.ai.agent_v2 import capabilities_for_report_files
        caps = capabilities_for_report_files(has_files=True)
        names = {t["name"] for t in reg.get_catalog_for_plan_type(
            "research", available_capabilities=set(caps),
        )}
        assert "read_file" in names and "grep_files" in names

    def test_absent_without_files_or_connector(self):
        from app.ai.registry import ToolRegistry

        reg = ToolRegistry()
        names = {t["name"] for t in reg.get_catalog_for_plan_type(
            "research", available_capabilities=set(),
        )}
        assert "read_file" not in names and "grep_files" not in names
