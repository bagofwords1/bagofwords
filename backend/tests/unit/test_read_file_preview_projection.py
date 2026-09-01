"""The UI preview contract must survive the persisted projection.

Report read endpoints do NOT serve `tool_execution.result_json` — the column is
deferred and the durable `context_summary_json` projection is served in its
place (see report_payload_projection.hydrate_tool_results_for_ui). A field
missing from that projection's allowlist is therefore invisible to the card the
moment a finished turn is refetched, even though the tool returned it and the
database still holds it.

That is exactly how the document preview disappeared: the live SSE payload
carried `preview` and rendered the PDF, then the refetch served a projection
without it and the card fell back to plain text. These tests pin both halves so
the two payloads cannot drift apart again.
"""
from __future__ import annotations

import pytest

from app.ai.persisted_summary import (
    SUMMARIZED_TOOL_NAMES,
    build_tool_context_summary,
)

PREVIEW = {
    "kind": "pdf",
    "file_id": "SESSION-FILE-1",
    "mime": "application/pdf",
    "target_page": 7,
    "pages_total": 42,
    "image_file_ids": None,
    "truncated": False,
}


def _read_result(**overrides):
    base = {
        "success": True,
        "connection_id": "C1",
        "file_id": "book.pdf",
        "file_name": "book.pdf",
        "path": "book.pdf",
        "content_type": "text",
        "text": "page body",
        "session_file_id": "SESSION-FILE-1",
        "preview": dict(PREVIEW),
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("tool_name", ["read_file", "read_email", "read_note"])
def test_preview_survives_the_projection(tool_name):
    """Without this the card renders text after every refetch."""
    projected = build_tool_context_summary(tool_name, _read_result())
    assert projected["preview"] == PREVIEW


def test_preview_is_copied_verbatim_not_summarized():
    """`preview` is a render contract, not a data payload — truncating or
    reshaping any field silently points the viewer at the wrong file or page."""
    projected = build_tool_context_summary("read_file", _read_result())
    assert projected["preview"]["file_id"] == "SESSION-FILE-1"
    assert projected["preview"]["target_page"] == 7
    assert projected["preview"]["mime"] == "application/pdf"


def test_image_preview_keeps_its_gallery_ids():
    result = _read_result(
        content_type="images",
        preview={**PREVIEW, "kind": "image", "image_file_ids": ["P1", "P2", "P3"]},
    )
    projected = build_tool_context_summary("read_file", result)
    assert projected["preview"]["image_file_ids"] == ["P1", "P2", "P3"]


def test_a_read_without_a_preview_projects_cleanly():
    """Older executions predate the contract; their absence must not add a key
    the frontend would then treat as a real (empty) preview."""
    result = _read_result()
    result.pop("preview")
    projected = build_tool_context_summary("read_file", result)
    assert "preview" not in projected


def test_bulky_fields_are_still_bounded():
    """The projection exists to stay small — adding `preview` must not have
    relaxed the excerpt caps that justify serving it instead of result_json."""
    from app.ai.persisted_summary import UI_FILE_PREVIEW_CHARS

    projected = build_tool_context_summary(
        "read_file", _read_result(text="x" * (UI_FILE_PREVIEW_CHARS * 3))
    )
    assert len(projected["text"]) == UI_FILE_PREVIEW_CHARS


def test_read_file_is_actually_a_projected_tool():
    """If read_file ever leaves this set the projection stops being consulted,
    and these tests would pass while proving nothing."""
    assert "read_file" in SUMMARIZED_TOOL_NAMES
