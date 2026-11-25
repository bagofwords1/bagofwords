"""Unit tests for delivering a create_doc document to WhatsApp as a PDF.

``create_doc`` writes a markdown document artifact to the report; a chat channel
got only prose, so a WhatsApp user who asked for "the analysis as a PDF" received
nothing to open. ``_send_doc_pdf_for_block`` renders that artifact through the
existing ``ReportPdfService`` and ships the PDF as a document.

Deterministic: the Chromium render (existing, separately-tested infra) is mocked;
these cover the new wiring — the create_doc trigger, the WhatsApp scope gate,
per-doc dedupe (the render is expensive), the render-failure path, and the
adapter call shape.
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import completion_block as cb


def _db_returning(*results):
    db = AsyncMock()
    outs = []
    for value in results:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        outs.append(r)
    db.execute = AsyncMock(side_effect=outs)
    db.get = AsyncMock(return_value=None)  # no User viewer by default
    return db


def _te(tool_name="create_doc", success=True, result_json=None):
    if result_json is None:
        result_json = {"success": True, "doc_id": "ver-1", "title": "Product Analysis"}
    return SimpleNamespace(tool_name=tool_name, success=success, result_json=result_json)


_SEQ = iter(range(1, 10_000))


def _completion(platform="whatsapp", user="+15551234567", cid=None, user_id=None):
    cid = cid or f"comp-{next(_SEQ)}"
    return SimpleNamespace(external_platform=platform, external_user_id=user, id=cid, user_id=user_id)


def _platform():
    return SimpleNamespace(platform_type="whatsapp", organization_id="org-1")


@pytest.fixture(autouse=True)
def _clear_dedupe():
    cb._sent_doc_pdf_keys.clear()
    yield
    cb._sent_doc_pdf_keys.clear()


@pytest.fixture
def _pdf_file():
    f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    f.write(b"%PDF-1.4 test")
    f.close()
    yield f.name
    try:
        os.unlink(f.name)
    except OSError:
        pass


async def _run(te=None, completion=None, db=None, adapter=None, pdf_path="__set__", _pdf_file=None):
    te = te if te is not None else _te()
    completion = completion or _completion()
    if db is None:
        db = _db_returning(_platform())
    if adapter is None:
        adapter = AsyncMock()
        adapter.send_file_in_thread = AsyncMock(return_value=True)
    if pdf_path == "__set__":
        pdf_path = _pdf_file
    svc = MagicMock()
    svc.generate_for_artifact = AsyncMock(return_value=pdf_path)
    with patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter), \
         patch("app.services.report_pdf_service.ReportPdfService", return_value=svc):
        result = await cb._send_doc_pdf_for_block(
            db, completion, te, "block-1", "thread-9", None, "org-1"
        )
    return result, adapter, db, svc


@pytest.mark.asyncio
async def test_doc_is_rendered_and_sent_as_pdf(_pdf_file):
    result, adapter, _, svc = await _run(_pdf_file=_pdf_file)
    assert result is True
    svc.generate_for_artifact.assert_awaited_once()
    assert svc.generate_for_artifact.call_args.args[0] == "ver-1"  # the ArtifactVersion id
    adapter.send_file_in_thread.assert_awaited_once()
    args, kwargs = adapter.send_file_in_thread.call_args
    assert args[0] == "+15551234567"
    assert args[1] == _pdf_file
    assert args[2] == "Product Analysis"      # caption = title
    assert kwargs["filename"] == "Product Analysis.pdf"
    assert kwargs["channel_id"] is None


@pytest.mark.asyncio
async def test_non_create_doc_tool_is_ignored():
    result, adapter, db, svc = await _run(te=_te(tool_name="attach_file"))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    svc.generate_for_artifact.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_tool_is_ignored():
    result, adapter, db, _ = await _run(te=_te(success=False))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_doc_id_is_ignored():
    result, adapter, db, _ = await _run(te=_te(result_json={"success": True, "title": "x"}))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_whatsapp_platform_is_ignored():
    result, adapter, db, svc = await _run(completion=_completion(platform="slack"))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    svc.generate_for_artifact.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_failure_is_handled_without_send():
    # Chromium render returns None → block handled, nothing sent, no crash.
    result, adapter, _, svc = await _run(pdf_path=None)
    assert result is True
    svc.generate_for_artifact.assert_awaited_once()
    adapter.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_doc_not_rendered_twice(_pdf_file):
    comp = _completion()
    r1, adapter1, _, svc1 = await _run(completion=comp, _pdf_file=_pdf_file)
    assert r1 is True
    svc1.generate_for_artifact.assert_awaited_once()
    # Second pass (transcript rebuild): dedupe key set → no second render/send.
    db2 = _db_returning(_platform())
    adapter2 = AsyncMock()
    adapter2.send_file_in_thread = AsyncMock(return_value=True)
    svc2 = MagicMock()
    svc2.generate_for_artifact = AsyncMock(return_value=_pdf_file)
    with patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter2), \
         patch("app.services.report_pdf_service.ReportPdfService", return_value=svc2):
        r2 = await cb._send_doc_pdf_for_block(db2, comp, _te(), "block-1", "thr", None, "org-1")
    assert r2 is True
    svc2.generate_for_artifact.assert_not_awaited()
    adapter2.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_viewer_resolved_from_completion_user(_pdf_file):
    # The render runs as the user the completion executed for (RLS on live charts).
    user_obj = SimpleNamespace(id="user-9")
    db = _db_returning(_platform())
    db.get = AsyncMock(return_value=user_obj)
    comp = _completion(user_id="user-9")
    _, _, _, svc = await _run(completion=comp, db=db, _pdf_file=_pdf_file)
    assert svc.generate_for_artifact.call_args.kwargs["viewer"] is user_obj


@pytest.mark.asyncio
async def test_no_platform_configured_returns_false():
    db = _db_returning(None)  # ExternalPlatform lookup empty
    result, adapter, _, svc = await _run(db=db)
    assert result is False
    svc.generate_for_artifact.assert_not_awaited()
    adapter.send_file_in_thread.assert_not_awaited()


def test_whatsapp_directive_mentions_create_doc_pdf():
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3

    planner_input = SimpleNamespace(external_platform="whatsapp")
    directive = PromptBuilderV3._platform_system_directives(planner_input)
    assert "create_doc" in directive
    assert "PDF" in directive
