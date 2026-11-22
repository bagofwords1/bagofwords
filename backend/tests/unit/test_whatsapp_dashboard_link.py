"""WhatsApp gets a link to a dashboard the agent built.

A dashboard lives in BOW; on a phone the user only read the agent's
description of it. A finished create_artifact / edit_artifact block now sends
a link to the dashboard's draft page (/d/{id}/draft): editors see the newest
version, a signed-out phone browser gets the sign-in prompt first, others are
forwarded to the published page.

Drives the block sender with the real WhatsAppAdapter against a fake Graph
API (httpx.MockTransport — Meta is the boundary) and asserts what WhatsApp
receives. The two DB lookups (title, platform) are stubbed in call order, as
in the sibling completion_block tests.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest

from app.models import completion_block as cb
from app.services.platform_adapters import whatsapp_adapter as whatsapp_adapter_mod
from app.services.platform_adapters.whatsapp_adapter import WhatsAppAdapter
from app.settings.config import settings

DASHBOARD = "dash-91c2"


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    cb._recent_dashboard_links.clear()
    yield
    cb._recent_dashboard_links.clear()


def _db(title="Top artists", platform=...):
    platform = SimpleNamespace(platform_type="whatsapp", organization_id="org-1") if platform is ... else platform
    outs = []
    for value in (title, platform):
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        outs.append(r)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=outs)
    return db


def _te(tool_name="create_artifact", success=True, result_json=None, created_step_id=None):
    if result_json is None:
        result_json = {"success": True, "artifact_id": "version-1", "resource_artifact_id": DASHBOARD}
    return SimpleNamespace(tool_name=tool_name, success=success, result_json=result_json,
                           created_step_id=created_step_id)


def _completion(platform="whatsapp", user="15551234567"):
    return SimpleNamespace(external_platform=platform, external_user_id=user, report_id="rep-7f3a")


@pytest.fixture
def graph(monkeypatch):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    original = httpx.AsyncClient

    class _Client(original):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(whatsapp_adapter_mod.httpx, "AsyncClient", _Client)
    return sent


def _adapter():
    platform = SimpleNamespace(
        platform_config={"phone_number_id": "PNID_1"},
        decrypt_credentials=lambda: {"access_token": "EAAtoken", "phone_number_id": "PNID_1"},
    )
    return WhatsAppAdapter(platform)


async def _send(te=None, completion=None, db=None):
    """What the block sender does for one finished tool block."""
    step_sender = AsyncMock()
    with patch.object(cb.PlatformAdapterFactory, "create_adapter", return_value=_adapter()), \
            patch.object(cb, "send_step_result_to_slack", step_sender):
        handled = await cb._send_tool_output_for_block(
            db or _db(), completion or _completion(), te or _te(), "block-1", "thread-9", None, "org-1",
        )
    return handled, step_sender


def _urls(text):
    return [w for w in text.split() if w.startswith("http")]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["create_artifact", "edit_artifact"])
@pytest.mark.parametrize("title", ["Top artists", "אמנים מובילים במכירות"])
async def test_dashboard_link_opens_its_draft_page(graph, tool_name, title):
    handled, step_sender = await _send(te=_te(tool_name=tool_name), db=_db(title=title))

    assert handled is True
    step_sender.assert_not_awaited()
    assert len(graph) == 1
    body = graph[0]["text"]["body"]
    assert f"*{title}*" in body
    urls = _urls(body)
    assert len(urls) == 1, "the URL must be bare so WhatsApp turns it into a link"
    url = urlparse(urls[0])
    assert f"{url.scheme}://{url.netloc}" == settings.bow_config.base_url.rstrip("/")
    assert url.path == f"/d/{DASHBOARD}/draft"
    assert not url.query


@pytest.mark.asyncio
async def test_create_then_edit_of_one_dashboard_sends_one_link(graph):
    await _send(te=_te("create_artifact"))
    await _send(te=_te("edit_artifact"))
    await _send(te=_te("edit_artifact"))

    assert len(graph) == 1


@pytest.mark.asyncio
async def test_two_dashboards_get_a_link_each(graph):
    await _send(te=_te(result_json={"success": True, "resource_artifact_id": "dash-a"}))
    await _send(te=_te(result_json={"success": True, "resource_artifact_id": "dash-b"}))

    paths = [urlparse(_urls(m["text"]["body"])[0]).path for m in graph]
    assert paths == ["/d/dash-a/draft", "/d/dash-b/draft"]


@pytest.mark.asyncio
async def test_the_same_dashboard_for_another_user_gets_its_own_link(graph):
    await _send(completion=_completion(user="15550000001"))
    await _send(completion=_completion(user="15550000002"))

    assert len(graph) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "", "   ", "Untitled Artifact"])
async def test_a_dashboard_without_a_real_title_is_called_dashboard(graph, stored):
    await _send(db=_db(title=stored))

    assert graph[0]["text"]["body"].startswith("*Dashboard*\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Q1 *top* sellers", "a_b_c", "~old~ vs new", "`code` view"])
async def test_title_markers_cannot_break_the_bold(graph, title):
    await _send(db=_db(title=title))

    first_line = graph[0]["text"]["body"].split("\n")[0]
    inner = first_line[1:-1]
    assert first_line.startswith("*") and first_line.endswith("*")
    assert not any(m in inner for m in "*_~`")


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "teams", "google_chat"])
async def test_other_channels_get_no_dashboard_link(graph, platform):
    handled, step_sender = await _send(completion=_completion(platform=platform))

    assert graph == []
    step_sender.assert_not_awaited()
    assert handled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("te", [
    _te(success=False),                                   # creation failed / stopped
    _te(result_json={"success": True}),                   # no dashboard id
], ids=["failed", "no-dashboard-id"])
async def test_no_link_and_nothing_else_without_a_built_dashboard(graph, te):
    handled, step_sender = await _send(te=te)

    assert handled is False
    assert graph == []
    step_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_chart_still_goes_out_as_the_chart(graph):
    handled, step_sender = await _send(
        te=_te(tool_name="create_data", result_json={"success": True}, created_step_id="chart-step-1"),
    )

    assert handled is True
    step_sender.assert_awaited_once()
    assert step_sender.call_args.args[0] == "chart-step-1"
    assert graph == []
