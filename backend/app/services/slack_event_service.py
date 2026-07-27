"""Transport-agnostic Slack inbound event handling.

Both Slack transports deliver the same ``event_callback`` JSON:
- the Events API webhook (`app/routes/slack_webhook.py`), and
- Socket Mode envelopes (`app/services/slack_socket_service.py`).

This module owns everything after parsing: event gating, dedupe, assistant
(Agent experience) handling, org routing, and the ExternalPlatformManager
handoff — so the two transports stay thin shims.
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_platform import ExternalPlatform
from app.services.external_platform_manager import ExternalPlatformManager
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory

logger = logging.getLogger(__name__)

platform_manager = ExternalPlatformManager()

# Simple in-memory dedupe by Slack event_id (Slack redelivers on slow acks;
# Socket Mode redelivers unacked envelopes). Same tradeoff as the other
# platform routes: per-process, cleared when it grows.
processed_events: set = set()

# Assistant (Agent experience) threads seen this process, as
# "{channel}:{thread_ts}". Used to show the native "is thinking…" status for
# messages in assistant threads. Best-effort: lost on restart, in which case
# users just don't get the status line until they open a new thread.
_assistant_threads: set = set()


async def find_platform_by_team_id(db: AsyncSession, team_id: str) -> Optional[ExternalPlatform]:
    """Find the Slack platform for a workspace team id."""
    stmt = select(ExternalPlatform).where(ExternalPlatform.platform_type == "slack")
    result = await db.execute(stmt)
    for platform in result.scalars().all():
        if (platform.platform_config or {}).get("team_id") == team_id:
            return platform
    return None


async def _suggested_prompts_for_org(db: AsyncSession, organization_id: str) -> list:
    """Collect conversation starters from the org's public agents (max 4)."""
    from app.models.data_source import DataSource

    stmt = select(DataSource).where(
        DataSource.organization_id == organization_id,
        DataSource.is_active == True,  # noqa: E712
        DataSource.is_public == True,  # noqa: E712
    )
    result = await db.execute(stmt)
    prompts = []
    for ds in result.scalars().all():
        if not ds.is_available_in("slack"):
            continue
        for starter in (ds.conversation_starters or []):
            text = starter.get("prompt") if isinstance(starter, dict) else starter
            if text and text not in prompts:
                prompts.append(text)
            if len(prompts) >= 4:
                return prompts
    return prompts


async def _handle_assistant_thread_started(
    db: AsyncSession, platform: ExternalPlatform, event: Dict[str, Any]
) -> Dict[str, Any]:
    """A user opened the app's assistant pane: remember the thread and push
    suggested prompts sourced from the org's agents' conversation starters."""
    thread = event.get("assistant_thread") or {}
    channel_id = thread.get("channel_id")
    thread_ts = thread.get("thread_ts")
    if not channel_id or not thread_ts:
        return {"ok": True}

    _assistant_threads.add(f"{channel_id}:{thread_ts}")
    if len(_assistant_threads) > 2000:
        _assistant_threads.clear()
        _assistant_threads.add(f"{channel_id}:{thread_ts}")

    adapter = PlatformAdapterFactory.create_adapter(platform)
    prompts = await _suggested_prompts_for_org(db, platform.organization_id)
    if prompts:
        await adapter.set_suggested_prompts(channel_id, thread_ts, prompts)
    return {"ok": True, "action": "assistant_thread_started"}


def is_assistant_thread(channel_id: Optional[str], thread_ts: Optional[str]) -> bool:
    return bool(channel_id and thread_ts and f"{channel_id}:{thread_ts}" in _assistant_threads)


async def handle_slack_event(db: AsyncSession, event_data: Dict[str, Any]) -> Dict[str, Any]:
    """Process one parsed Slack ``event_callback`` payload.

    Returns a small status dict; never raises for routine skips. The caller
    (webhook route / socket client) has already acked transport-level
    concerns (URL challenge, envelope ack, signature).
    """
    team_id = event_data.get("team_id")
    if not team_id:
        return {"ok": False, "error": "No team_id in event"}

    platform = await find_platform_by_team_id(db, team_id)
    if not platform or not platform.is_active:
        logger.info("SLACK: no active platform for team_id %s", team_id)
        return {"ok": True, "skipped": "no_platform"}

    if event_data.get("type") != "event_callback":
        return {"ok": True, "skipped": "not_event_callback"}

    event = event_data.get("event", {})
    event_type = event.get("type")

    # Agent experience events.
    # assistant_view (legacy assistant pane): assistant_thread_started fires
    # when the pane opens; prompts render inside the thread.
    # agent_view (2026 Agent messaging experience): assistant_thread_started
    # never fires — app_home_opened(tab=messages) is the "user is here"
    # signal, and prompts render at the top of the Messages tab (no
    # thread_ts).
    if event_type == "assistant_thread_started":
        return await _handle_assistant_thread_started(db, platform, event)
    if event_type == "assistant_thread_context_changed":
        return {"ok": True, "skipped": "assistant_context"}
    if event_type == "app_home_opened":
        if event.get("tab") == "messages" and event.get("channel"):
            adapter = PlatformAdapterFactory.create_adapter(platform)
            prompts = await _suggested_prompts_for_org(db, platform.organization_id)
            if prompts:
                await adapter.set_suggested_prompts(event["channel"], None, prompts)
            return {"ok": True, "action": "app_home_opened"}
        return {"ok": True, "skipped": "app_home_tab"}

    # Ignore bot echoes (including our own replies)
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return {"ok": True, "skipped": "bot_message"}

    if event_type not in ("message", "app_mention"):
        return {"ok": True, "skipped": f"event_type:{event_type}"}

    # Dedupe by event_id
    event_id = event_data.get("event_id")
    if event_id in processed_events:
        return {"ok": True, "skipped": "duplicate"}
    processed_events.add(event_id)
    if len(processed_events) > 1000:
        processed_events.clear()

    # DMs only for plain messages; app_mention comes from channels
    if event_type == "message" and event.get("channel_type") != "im":
        return {"ok": True, "skipped": "non_dm_message"}

    if not (event.get("text") or "").strip():
        return {"ok": True, "skipped": "empty"}

    # Native "is thinking…" status (auto-clears when a reply is posted into
    # the thread). In agent_view this also auto-opens the reply thread under
    # the user's message, so we set it for EVERY DM using the effective
    # thread (thread_ts for replies, the message's own ts for top-level
    # messages — which is where our reply will thread). Apps without the
    # Agent experience just get a swallowed API error. Best-effort — never
    # blocks handling.
    channel_id = event.get("channel")
    thread_ts = event.get("thread_ts")
    if event_type == "message" and channel_id:
        effective_thread = thread_ts or event.get("ts")
        if effective_thread:
            try:
                adapter = PlatformAdapterFactory.create_adapter(platform)
                await adapter.set_assistant_status(channel_id, effective_thread, "is thinking…")
            except Exception as e:  # noqa: BLE001
                logger.debug("SLACK: setStatus failed: %s", e)

    result = await platform_manager.handle_incoming_message(
        db, "slack", platform.organization_id, event_data
    )
    return {"ok": True, "result": result}
