"""Unit tests for delivering generate_image output to chat platforms.

``generate_image`` stores its picture as a report File (returning a ``file_id``),
not a Step, so the chart/table step path (``send_step_result_to_slack``) never
picks it up. ``_send_generated_image_for_block`` is the parallel path that ships
the generated image as an image attachment. These tests are deterministic — no
network, no real DB — exercising the branch logic and the adapter call shape.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import completion_block as cb


def _db_returning(*results):
    """An AsyncMock db whose execute() yields one result object per call.

    Each result's scalar_one_or_none() returns the paired value.
    """
    db = AsyncMock()
    outs = []
    for value in results:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        outs.append(r)
    db.execute = AsyncMock(side_effect=outs)
    return db


def _te(tool_name="generate_image", success=True, result_json=None):
    if result_json is None:
        result_json = {"success": True, "file_id": "file-1", "filename": "city.png"}
    return SimpleNamespace(tool_name=tool_name, success=success, result_json=result_json)


def _completion(platform="whatsapp", user="+15551234567"):
    return SimpleNamespace(external_platform=platform, external_user_id=user)


def _file(path="uploads/files/abc_city.png", filename="city.png", content_type="image/png"):
    return SimpleNamespace(id="file-1", path=path, filename=filename, content_type=content_type)


async def _run(te=None, file_row=..., platform=..., completion=None):
    """Drive _send_generated_image_for_block with a mocked db + adapter.

    Returns (result, adapter, db). file_row/platform default to a valid image
    File and a whatsapp ExternalPlatform; pass None to simulate a missing row.
    """
    te = te if te is not None else _te()
    file_row = _file() if file_row is ... else file_row
    platform = SimpleNamespace(platform_type="whatsapp", organization_id="org-1") if platform is ... else platform
    completion = completion or _completion()

    # execute() is called for File first, then (if reached) ExternalPlatform.
    db = _db_returning(file_row, platform)
    adapter = AsyncMock()
    adapter.send_file_in_thread = AsyncMock(return_value=True)

    with patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter):
        result = await cb._send_generated_image_for_block(
            db, completion, te, "block-1", "thread-9", None, "org-1"
        )
    return result, adapter, db


@pytest.mark.asyncio
async def test_generated_image_is_sent_as_image():
    result, adapter, _ = await _run()
    assert result is True
    adapter.send_file_in_thread.assert_awaited_once()
    args, kwargs = adapter.send_file_in_thread.call_args
    # (user_id, file_path, caption, thread_ts, channel_id=...)
    assert args[0] == "+15551234567"
    assert args[1] == "uploads/files/abc_city.png"
    assert args[2] == "city.png"  # caption = filename
    assert args[3] == "thread-9"
    assert kwargs["channel_id"] is None


@pytest.mark.asyncio
async def test_non_generate_image_tool_is_ignored():
    result, adapter, db = await _run(te=_te(tool_name="create_data"))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()  # bails before any DB lookup


@pytest.mark.asyncio
async def test_failed_generation_is_ignored():
    result, adapter, db = await _run(te=_te(success=False))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_file_id_is_ignored():
    result, adapter, db = await _run(te=_te(result_json={"success": True}))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_image_file_is_not_sent():
    # A row that isn't an image (e.g. a CSV) must not be shipped as a picture.
    result, adapter, _ = await _run(file_row=_file(content_type="text/csv"))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_file_row_is_ignored():
    result, adapter, _ = await _run(file_row=None)
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_platform_configured_returns_false():
    result, adapter, _ = await _run(platform=None)
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_send_failure_still_marks_handled():
    # A transient send error must not cause repeated re-uploads: the attempt is
    # made once and the function returns True so the caller sets the dedupe guard.
    te = _te()
    db = _db_returning(_file(), SimpleNamespace(platform_type="whatsapp", organization_id="org-1"))
    adapter = AsyncMock()
    adapter.send_file_in_thread = AsyncMock(side_effect=RuntimeError("graph 500"))
    with patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter):
        result = await cb._send_generated_image_for_block(
            db, _completion(), te, "block-1", None, None, "org-1"
        )
    assert result is True
    adapter.send_file_in_thread.assert_awaited_once()


def test_generate_image_tool_available_on_whatsapp():
    """The tool must not be platform-filtered out of the WhatsApp catalog."""
    from app.ai.registry import ToolRegistry

    registry = ToolRegistry()
    names = {
        t["name"] if isinstance(t, dict) else t.metadata.name
        for t in registry.get_catalog_for_plan_type(plan_type="action", platform="whatsapp")
    }
    assert "generate_image" in names


def test_whatsapp_directive_mentions_generated_images_and_guardrail():
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3

    planner_input = SimpleNamespace(external_platform="whatsapp")
    directive = PromptBuilderV3._platform_system_directives(planner_input)
    assert "generate_image" in directive
    # Anti-hallucination guardrail present.
    assert "never claim" in directive.lower()
