"""Channel delivery — mock-mode verification for all four channels.

Verifies (without live Teams/Slack/SMTP):
  - delivery to teams/slack/ai_mailbox/smtp records to the mock outbox
  - plain SMTP body is plain-text, human-sounding, and carries a continue link
  - chat-channel deliveries capture an external user id

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db BOW_CHANNELS_MOCK=1 \
      .venv/bin/python -m pytest tests/e2e/test_channel_delivery.py -v -s
"""
import os
import uuid
import asyncio
import tempfile

import pytest

os.environ["BOW_CHANNELS_MOCK"] = "1"
os.environ["BOW_CHANNELS_MOCK_FILE"] = os.path.join(tempfile.gettempdir(), f"bow_outbox_{uuid.uuid4().hex}.json")

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.services.channel_delivery_service import (
    channel_delivery_service, read_mock_outbox, clear_mock_outbox,
)


async def _seed():
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Deliver Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name="Dana", email=f"dana-{suffix}@example.com",
                    hashed_password="x", is_active=True, is_verified=True)
        db.add(user)
        await db.commit()
        return org.id, user.id


@pytest.mark.asyncio
async def test_delivery_all_channels_mock():
    clear_mock_outbox()
    org_id, user_id = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)

        for channel in ("teams", "slack", "ai_mailbox", "smtp"):
            res = await channel_delivery_service.deliver(
                db, org, user, channel,
                title="Weekly Forecast",
                content="Sales are up 12% week over week.\nTop customer: Acme.",
                report_url="http://localhost:3000/reports/abc123",
                report_id="abc123",
            )
            print(f"[deliver] {channel} -> status={res.status} used={res.used_channel} mock={res.mock}")
            assert res.status == "sent", f"{channel} should be sent in mock mode"
            assert res.mock is True

        outbox = read_mock_outbox()
        channels = [e["channel"] for e in outbox]
        print(f"[outbox] channels={channels}")
        assert set(channels) == {"teams", "slack", "ai_mailbox", "smtp"}

        # plain SMTP body: human greeting + continue link, no HTML
        smtp = next(e for e in outbox if e["channel"] == "smtp")
        assert smtp["body"].startswith("Hi Dana"), "SMTP body should be human-like"
        assert "continue this discussion" in smtp["body"].lower()
        assert "<" not in smtp["body"], "SMTP body must be plain text (no HTML)"
        print("[smtp] plain human body + continue link OK")

        # chat channels capture an external user id
        teams = next(e for e in outbox if e["channel"] == "teams")
        assert teams["external_user_id"]
        print(f"[teams] external_user_id={teams['external_user_id']}")

    clear_mock_outbox()
