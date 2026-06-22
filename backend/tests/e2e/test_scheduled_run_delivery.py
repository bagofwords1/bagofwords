"""Scheduled run: new_report cloning + channel delivery (LLM stubbed).

Verifies the scheduled_run_prompt path end-to-end without a live LLM:
  - run_mode='new_report' clones a fresh report grouped under the task
    (Report.source_scheduled_prompt_id)
  - the run delivers to the subscription's channel (mock Teams outbox)

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db BOW_CHANNELS_MOCK=1 \
      .venv/bin/python -m pytest tests/e2e/test_scheduled_run_delivery.py -v -s
"""
import os
import uuid
import tempfile
from types import SimpleNamespace

import pytest

os.environ["BOW_CHANNELS_MOCK"] = "1"
os.environ["BOW_CHANNELS_MOCK_FILE"] = os.path.join(tempfile.gettempdir(), f"bow_outbox_run_{uuid.uuid4().hex}.json")

from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.report import Report
from app.models.scheduled_prompt import ScheduledPrompt
from app.services.scheduled_prompt_service import scheduled_prompt_service
from app.services.completion_service import CompletionService
from app.services.channel_delivery_service import read_mock_outbox, clear_mock_outbox


def _fake_response(text: str):
    block = SimpleNamespace(tool_execution=None, content=text)
    comp = SimpleNamespace(role="system", completion_blocks=[block])
    return SimpleNamespace(completions=[comp])


async def _seed_subscription(run_mode: str, channel: str):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Run Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name="Sam", email=f"sam-{suffix}@example.com",
                    hashed_password="x", is_active=True, is_verified=True)
        db.add(user)
        await db.flush()
        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id, is_active=True, owner_user_id=user.id)
        db.add(ds)
        await db.flush()
        # anchor report
        anchor = Report(title="Daily Ops", slug=f"daily-ops-{suffix}", user_id=user.id, organization_id=org.id)
        db.add(anchor)
        await db.flush()
        sp = ScheduledPrompt(
            report_id=anchor.id, user_id=user.id,
            prompt={"content": "Summarize ops", "mode": "chat"},
            cron_schedule="0 9 * * 1", is_active=True,
            channel=channel, run_mode=run_mode,
        )
        db.add(sp)
        await db.commit()
        return org.id, user.id, anchor.id, sp.id


@pytest.mark.asyncio
async def test_new_report_run_and_delivery(monkeypatch):
    clear_mock_outbox()
    org_id, user_id, anchor_id, sp_id = await _seed_subscription("new_report", "teams")

    async def _fake_create_completion(self, *args, **kwargs):
        return _fake_response("Ops are healthy. 3 incidents resolved.")
    monkeypatch.setattr(CompletionService, "create_completion", _fake_create_completion)

    await scheduled_prompt_service.scheduled_run_prompt(sp_id)

    async with async_session_maker() as db:
        # a fresh report was created and grouped under the task
        rows = await db.execute(
            select(Report).filter(Report.source_scheduled_prompt_id == sp_id)
        )
        run_reports = list(rows.scalars().all())
        print(f"[new_report] run reports created: {len(run_reports)}")
        assert len(run_reports) == 1, "new_report mode should clone one report per run"
        assert run_reports[0].id != anchor_id

    outbox = read_mock_outbox()
    teams = [e for e in outbox if e["channel"] == "teams"]
    print(f"[delivery] teams deliveries: {len(teams)}")
    assert len(teams) == 1, "the run should deliver to teams"
    assert "Ops are healthy" in teams[0]["body"]
    print(f"[delivery] body preview: {teams[0]['body'][:60]!r}")
    clear_mock_outbox()


@pytest.mark.asyncio
async def test_append_mode_reuses_report(monkeypatch):
    clear_mock_outbox()
    org_id, user_id, anchor_id, sp_id = await _seed_subscription("append", "smtp")

    async def _fake_create_completion(self, *args, **kwargs):
        # assert it runs against the anchor report (append mode)
        assert kwargs.get("report_id") == anchor_id
        return _fake_response("Weekly numbers attached.")
    monkeypatch.setattr(CompletionService, "create_completion", _fake_create_completion)

    await scheduled_prompt_service.scheduled_run_prompt(sp_id)

    async with async_session_maker() as db:
        rows = await db.execute(
            select(Report).filter(Report.source_scheduled_prompt_id == sp_id)
        )
        assert len(list(rows.scalars().all())) == 0, "append mode must NOT create new reports"

    outbox = read_mock_outbox()
    # smtp delivery falls back to mock outbox under mock mode
    assert any(e["channel"] == "smtp" for e in outbox)
    print("[append] reused anchor report; smtp delivery recorded")
    clear_mock_outbox()
