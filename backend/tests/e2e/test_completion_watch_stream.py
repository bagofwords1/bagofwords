"""E2E contract tests for GET /api/reports/{report_id}/completions/{completion_id}/stream.

The watch endpoint lets a client that lost its kickoff SSE stream (page
refresh, network drop, second tab) re-attach to a completion. Contract under
test, with no LLM involved (completions are seeded directly):

  - Emits `completion.resumed` first, carrying the completion's status.
  - Replays every persisted block as an idempotent `block.upsert`.
  - Ends with `completion.finished` (same status) and `[DONE]` when the
    completion is terminal — a late watcher always converges, never hangs.
  - 404s when the completion doesn't belong to the report, and 401s without
    credentials — it is a read path, gated like reading completions.
"""

import asyncio
import json

import pytest

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.settings.database import create_async_database_engine


def _seed_terminal_completion(report_id: str, *, status: str = "success", content: str = "final answer text"):
    """Insert a system completion with one completed block, bypassing the LLM."""

    async def _seed():
        engine = create_async_database_engine()
        try:
            return await _seed_with_engine(engine)
        finally:
            await engine.dispose()

    async def _seed_with_engine(engine):
        async with AsyncSession(engine, expire_on_commit=False) as db:
            comp = Completion(
                prompt=None,
                completion={"content": content},
                model="test-model",
                report_id=report_id,
                role="system",
                status=status,
                turn_index=1,
                message_type="table",
            )
            db.add(comp)
            await db.flush()
            block = CompletionBlock(
                completion_id=str(comp.id),
                source_type="decision",
                block_index=100,
                loop_index=0,
                title="Planning (action)",
                status="completed",
                icon="🧠",
                content=content,
            )
            db.add(block)
            await db.commit()
            return str(comp.id), str(block.id)

    return asyncio.run(_seed())


def _read_stream(test_client, report_id: str, completion_id: str, headers: dict):
    """Consume the watch stream until [DONE]; return (events, saw_done)."""
    events, saw_done = [], False
    with test_client.stream(
        "GET",
        f"/api/reports/{report_id}/completions/{completion_id}/stream",
        headers=headers,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                saw_done = True
                break
            events.append(json.loads(payload))
    return events, saw_done


@pytest.mark.e2e
def test_watch_stream_replays_terminal_completion(
    test_client, create_user, login_user, whoami, create_report
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Watch stream report", user_token=token, org_id=org_id, data_sources=[])

    completion_id, block_id = _seed_terminal_completion(report["id"])

    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    events, saw_done = _read_stream(test_client, report["id"], completion_id, headers)

    assert saw_done, "watch stream on a terminal completion must close with [DONE]"
    names = [e["event"] for e in events]
    assert names[0] == "completion.resumed"
    assert events[0]["data"]["status"] == "success"
    assert "completion.finished" in names

    upserts = [e for e in events if e["event"] == "block.upsert"]
    assert any(
        u["data"]["block"]["id"] == block_id and u["data"]["block"].get("content")
        for u in upserts
    ), "persisted blocks must be replayed as block.upsert snapshots"

    finished = [e for e in events if e["event"] == "completion.finished"][-1]
    assert finished["data"]["status"] == "success"


@pytest.mark.e2e
def test_watch_stream_reports_error_status(
    test_client, create_user, login_user, whoami, create_report
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Watch stream error report", user_token=token, org_id=org_id, data_sources=[])

    completion_id, _ = _seed_terminal_completion(report["id"], status="error")

    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    events, saw_done = _read_stream(test_client, report["id"], completion_id, headers)

    assert saw_done
    assert events[0]["event"] == "completion.resumed"
    assert events[0]["data"]["status"] == "error"
    finished = [e for e in events if e["event"] == "completion.finished"][-1]
    assert finished["data"]["status"] == "error"


@pytest.mark.e2e
def test_watch_stream_404_when_completion_not_in_report(
    test_client, create_user, login_user, whoami, create_report
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report_a = create_report(title="Report A", user_token=token, org_id=org_id, data_sources=[])
    report_b = create_report(title="Report B", user_token=token, org_id=org_id, data_sources=[])

    completion_id, _ = _seed_terminal_completion(report_a["id"])

    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    resp = test_client.get(
        f"/api/reports/{report_b['id']}/completions/{completion_id}/stream",
        headers=headers,
    )
    assert resp.status_code == 404


@pytest.mark.e2e
def test_watch_stream_requires_auth(
    test_client, create_user, login_user, whoami, create_report
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Auth report", user_token=token, org_id=org_id, data_sources=[])
    completion_id, _ = _seed_terminal_completion(report["id"])

    resp = test_client.get(f"/api/reports/{report['id']}/completions/{completion_id}/stream")
    assert resp.status_code == 401
