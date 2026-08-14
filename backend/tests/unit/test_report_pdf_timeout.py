"""The PDF export must never hang forever.

A wedged Chromium (page.pdf() has no per-call timeout) used to hang the
awaiting request indefinitely — and with it every request queued on the
per-report export lock, leaving the share page stuck at "Exporting...".
generate_pdf now bounds the whole render with RENDER_TIMEOUT_SECONDS and
reports failure as None, which the route turns into an HTTP error.
"""

import asyncio

import pytest

from app.services.report_pdf_service import ReportPdfService


@pytest.mark.asyncio
async def test_generate_pdf_times_out_instead_of_hanging(monkeypatch):
    service = ReportPdfService()

    async def hang_forever(artifact_id, html_content):
        await asyncio.sleep(3600)

    monkeypatch.setattr(service, "_generate_pdf_inner", hang_forever)
    monkeypatch.setattr(ReportPdfService, "RENDER_TIMEOUT_SECONDS", 0.05)

    result = await asyncio.wait_for(
        service.generate_pdf("artifact-under-test", "<html></html>"),
        timeout=5,
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_pdf_returns_none_on_render_failure(monkeypatch):
    service = ReportPdfService()

    async def explode(artifact_id, html_content):
        raise RuntimeError("chromium crashed")

    monkeypatch.setattr(service, "_generate_pdf_inner", explode)

    assert await service.generate_pdf("artifact-under-test", "<html></html>") is None
