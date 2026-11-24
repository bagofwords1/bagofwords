"""Unit tests for delivering attach_file output to a chat channel.

``attach_file`` is the agent's explicit "this file is part of the answer"
signal — it materializes connector files as durable report Files. The web UI
lists them; ``_send_attached_files_for_block`` mirrors exactly those files to
the channel so a WhatsApp user who asked for a document receives it. Only
attached files are delivered — the many ``read_file`` research reads are not —
so the user never gets the rejected candidates the agent merely looked at.

Deterministic: no network, no real DB. Exercises the branch logic, the
WhatsApp-only scope gate, per-file dedupe, and the adapter call shape.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import completion_block as cb


def _db_returning(*results):
    """An AsyncMock db whose execute() yields one result per call, in order.

    First call resolves the ExternalPlatform, then one File row per sendable
    entry.
    """
    db = AsyncMock()
    outs = []
    for value in results:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        outs.append(r)
    db.execute = AsyncMock(side_effect=outs)
    return db


def _te(tool_name="attach_file", success=True, result_json=None):
    if result_json is None:
        result_json = {
            "success": True,
            "attached_count": 1,
            "files": [
                {"file_id": "conn/abc", "name": "lease.pdf",
                 "session_file_id": "file-1", "size": 1234},
            ],
        }
    return SimpleNamespace(tool_name=tool_name, success=success, result_json=result_json)


_COMPLETION_SEQ = iter(range(1, 10_000))


def _completion(platform="whatsapp", user="+15551234567", cid=None):
    # Unique id per completion so the module-level per-file dedupe set does not
    # leak between tests.
    cid = cid or f"comp-{next(_COMPLETION_SEQ)}"
    return SimpleNamespace(external_platform=platform, external_user_id=user, id=cid)


def _file(fid="file-1", path="uploads/files/abc_lease.pdf", filename="lease.pdf",
          content_type="application/pdf"):
    return SimpleNamespace(id=fid, path=path, filename=filename, content_type=content_type)


def _platform():
    return SimpleNamespace(platform_type="whatsapp", organization_id="org-1")


@pytest.fixture(autouse=True)
def _clear_dedupe():
    cb._sent_attached_file_keys.clear()
    yield
    cb._sent_attached_file_keys.clear()


async def _run(te=None, completion=None, db=None, adapter=None):
    te = te if te is not None else _te()
    completion = completion or _completion()
    if db is None:
        db = _db_returning(_platform(), _file())
    if adapter is None:
        adapter = AsyncMock()
        adapter.send_file_in_thread = AsyncMock(return_value=True)
    with patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter):
        result = await cb._send_attached_files_for_block(
            db, completion, te, "block-1", "thread-9", None, "org-1"
        )
    return result, adapter, db


@pytest.mark.asyncio
async def test_attached_file_is_sent():
    result, adapter, _ = await _run()
    assert result is True
    adapter.send_file_in_thread.assert_awaited_once()
    args, kwargs = adapter.send_file_in_thread.call_args
    # (user_id, file_path, caption, thread_ts, channel_id=...)
    assert args[0] == "+15551234567"
    assert args[1] == "uploads/files/abc_lease.pdf"
    assert args[2] == "lease.pdf"  # caption = filename
    assert args[3] == "thread-9"
    assert kwargs["channel_id"] is None
    # Clean display name passed through, not the uuid-prefixed storage basename.
    assert kwargs["filename"] == "lease.pdf"


@pytest.mark.asyncio
async def test_non_attach_file_tool_is_ignored():
    result, adapter, db = await _run(te=_te(tool_name="read_file"))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()  # bails before any DB lookup


@pytest.mark.asyncio
async def test_failed_tool_is_ignored():
    result, adapter, db = await _run(te=_te(success=False))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_files_is_ignored():
    te = _te(result_json={"success": True, "attached_count": 0, "files": []})
    result, adapter, db = await _run(te=te)
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_failed_attachments_is_ignored():
    # Entries with an error and no session_file_id must not resolve an adapter.
    te = _te(result_json={
        "success": False, "attached_count": 0,
        "files": [{"file_id": "conn/x", "error": "not found"}],
    })
    result, adapter, db = await _run(te=te)
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_whatsapp_platform_is_ignored():
    # The delivery loop serves every chat platform; the scope gate keeps
    # Slack/Teams/Google Chat behaviour unchanged for now.
    result, adapter, db = await _run(completion=_completion(platform="slack"))
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_any_file_type_is_sent():
    # Not gated on content_type: a document (not an image) is delivered too;
    # the adapter picks image vs document itself.
    db = _db_returning(_platform(), _file(content_type="text/csv", filename="rents.csv",
                                          path="uploads/files/x_rents.csv"))
    result, adapter, _ = await _run(db=db)
    assert result is True
    args, _ = adapter.send_file_in_thread.call_args
    assert args[1] == "uploads/files/x_rents.csv"


@pytest.mark.asyncio
async def test_multiple_files_all_sent():
    te = _te(result_json={
        "success": True, "attached_count": 2,
        "files": [
            {"file_id": "c/1", "name": "a.pdf", "session_file_id": "file-1", "size": 1},
            {"file_id": "c/2", "name": "b.pdf", "session_file_id": "file-2", "size": 2},
        ],
    })
    db = _db_returning(_platform(), _file(fid="file-1", filename="a.pdf"),
                       _file(fid="file-2", filename="b.pdf"))
    result, adapter, _ = await _run(te=te, db=db)
    assert result is True
    assert adapter.send_file_in_thread.await_count == 2


@pytest.mark.asyncio
async def test_same_file_not_sent_twice():
    # Re-running the same completion's attach block (transcript rebuild) must
    # not re-upload the file.
    comp = _completion()
    _, adapter1, _ = await _run(completion=comp)
    assert adapter1.send_file_in_thread.await_count == 1
    # Second pass: the per-file dedupe key is already set; nothing re-sent.
    db2 = _db_returning(_platform(), _file())
    adapter2 = AsyncMock()
    adapter2.send_file_in_thread = AsyncMock(return_value=True)
    with patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=adapter2):
        result = await cb._send_attached_files_for_block(
            db2, comp, _te(), "block-1", "thread-9", None, "org-1"
        )
    assert result is True  # still an attach_file block
    adapter2.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_file_row_is_skipped():
    db = _db_returning(_platform(), None)  # File lookup returns nothing
    result, adapter, _ = await _run(db=db)
    assert result is True  # handled the block
    adapter.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_platform_configured_returns_false():
    db = _db_returning(None)  # ExternalPlatform lookup returns nothing
    result, adapter, _ = await _run(db=db)
    assert result is False
    adapter.send_file_in_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_send_failure_still_marks_handled():
    # A transient send error must not cause repeated re-uploads: the file is
    # marked sent before the attempt and the function returns True.
    db = _db_returning(_platform(), _file())
    adapter = AsyncMock()
    adapter.send_file_in_thread = AsyncMock(side_effect=RuntimeError("graph 500"))
    result, adapter, _ = await _run(db=db, adapter=adapter)
    assert result is True
    adapter.send_file_in_thread.assert_awaited_once()


def test_attach_file_tool_available_on_whatsapp():
    """The tool must not be platform-filtered out of the WhatsApp catalog."""
    from app.ai.registry import ToolRegistry

    registry = ToolRegistry()
    names = {
        t["name"] if isinstance(t, dict) else t.metadata.name
        for t in registry.get_catalog_for_plan_type(plan_type="action", platform="whatsapp")
    }
    assert "attach_file" in names


def test_whatsapp_directive_mentions_attach_file_delivery():
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3

    planner_input = SimpleNamespace(external_platform="whatsapp")
    directive = PromptBuilderV3._platform_system_directives(planner_input)
    assert "attach_file" in directive
    # Teaches that attaching delivers, and that research reads do not.
    assert "deliver" in directive.lower()
