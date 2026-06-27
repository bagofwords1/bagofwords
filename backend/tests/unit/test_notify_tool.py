"""Unit tests for the notify tool wrapper (thin over NotifyService)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.tools.implementations.notify import NotifyTool
from app.ai.tools.schemas.notify import NotifyInput


def _ctx(**overrides):
    ctx = {
        "db": MagicMock(),
        "user": SimpleNamespace(id="u-self", email="me@acme.com", name="Me"),
        "organization": SimpleNamespace(id="org-1"),
        "report": SimpleNamespace(id="r-1"),
    }
    ctx.update(overrides)
    return ctx


async def _collect(tool, tool_input, ctx):
    return [e async for e in tool.run_stream(tool_input, ctx)]


def _end(events):
    return [e for e in events if e.type == "tool.end"][-1].payload


def test_notify_input_requires_subject_and_body():
    with pytest.raises(Exception):
        NotifyInput(subject="hi")  # missing body
    # recipients optional → empty means self-only
    assert NotifyInput(subject="hi", body="x").recipients == []


@pytest.mark.asyncio
async def test_notify_requires_context():
    tool = NotifyTool()
    events = await _collect(tool, {"subject": "s", "body": "b"}, _ctx(organization=None))
    out = _end(events)["output"]
    assert out["success"] is False
    assert "organization" in (out["error"] or "").lower()


@pytest.mark.asyncio
async def test_notify_happy_path_delegates_to_service():
    tool = NotifyTool()
    fake_result = {
        "success": True,
        "results": [
            {"email": "me@acme.com", "name": "Me", "is_self": True, "delivered": ["in_app", "email"], "failed": [], "error": None},
            {"email": "bob@acme.com", "name": "Bob", "is_self": False, "delivered": ["in_app", "teams"], "failed": [], "error": None},
        ],
        "rejected": ["outsider@evil.com"],
    }
    with patch("app.services.notify_service.notify_service.notify", new=AsyncMock(return_value=fake_result)) as mock_notify:
        events = await _collect(
            tool,
            {"subject": "Q2", "body": "done", "recipients": ["bob@acme.com", "outsider@evil.com"]},
            _ctx(),
        )
        mock_notify.assert_awaited_once()
        kw = mock_notify.await_args.kwargs
        assert kw["recipient_emails"] == ["bob@acme.com", "outsider@evil.com"]
        assert kw["subject"] == "Q2"

    payload = _end(events)
    out = payload["output"]
    assert out["success"] is True
    assert {r["email"] for r in out["results"]} == {"me@acme.com", "bob@acme.com"}
    assert out["rejected"] == ["outsider@evil.com"]
    assert payload["observation"]["success"] is True
    assert "skipped non-members" in payload["observation"]["summary"]
