"""Transport-agnostic Slack inbound event handling.

Both Slack transports deliver the same ``event_callback`` JSON:
- the Events API webhook (`app/routes/slack_webhook.py`), and
- Socket Mode envelopes (`app/services/slack_socket_service.py`).

This module owns everything after parsing: event gating, dedupe, assistant
(Agent experience) handling, org routing, and the ExternalPlatformManager
handoff — so the two transports stay thin shims.
"""

import logging
from typing import Any, Dict, List, Optional

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


def _routing_order(platform: ExternalPlatform):
    """Deterministic preference between platforms: active first, then oldest."""
    created = getattr(platform, "created_at", None)
    return (
        0 if platform.is_active else 1,
        created.isoformat() if created is not None else "",
        str(platform.id),
    )


async def find_platforms_by_team_id(db: AsyncSession, team_id: str) -> List[ExternalPlatform]:
    """Every Slack platform connected to a workspace, in every organization.

    A workspace can host several Slack apps (one per organization), so the
    team id alone does not identify a platform. Active platforms come first.
    """
    stmt = select(ExternalPlatform).where(ExternalPlatform.platform_type == "slack")
    result = await db.execute(stmt)
    matches = [
        platform for platform in result.scalars().all()
        if (platform.platform_config or {}).get("team_id") == team_id
    ]
    return sorted(matches, key=_routing_order)


async def find_platform_by_team_id(db: AsyncSession, team_id: str) -> Optional[ExternalPlatform]:
    """The Slack platform for a workspace, only when the workspace identifies
    exactly one. With several candidates the caller must disambiguate by app
    id / signature (see ``resolve_slack_candidates``)."""
    matches = await find_platforms_by_team_id(db, team_id)
    if len(matches) == 1:
        return matches[0]
    if matches:
        logger.warning(
            "SLACK: %d platforms share team_id %s; the team id alone cannot route this event",
            len(matches), team_id,
        )
    return None


async def resolve_slack_candidates(
    db: AsyncSession, team_id: Optional[str], api_app_id: Optional[str]
) -> List[ExternalPlatform]:
    """Platforms an inbound Events API request may belong to, best first.

    The request is only attributed to a candidate once its signature verifies
    against that candidate's signing secret (done by the route), so this only
    narrows the search: platforms known to be a *different* Slack app are
    dropped, and platforms whose stored app id matches come first. Platforms
    connected before the app id was recorded stay in the list and are
    settled by the signature.
    """
    if not team_id:
        return []
    matches = await find_platforms_by_team_id(db, team_id)
    if len(matches) <= 1:
        return matches

    logger.warning(
        "SLACK: %d platforms share team_id %s (organizations: %s); routing by app id",
        len(matches), team_id,
        ", ".join(sorted({str(p.organization_id) for p in matches})),
    )

    def stored_app_id(platform: ExternalPlatform) -> Optional[str]:
        return (platform.platform_config or {}).get("app_id")

    if api_app_id:
        exact = [p for p in matches if stored_app_id(p) == api_app_id]
        unknown = [p for p in matches if not stored_app_id(p)]
        return exact + unknown
    return matches


async def _load_slack_platform(db: AsyncSession, platform_id: str) -> Optional[ExternalPlatform]:
    stmt = select(ExternalPlatform).where(
        ExternalPlatform.id == str(platform_id),
        ExternalPlatform.platform_type == "slack",
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def _remember_app_id(db: AsyncSession, platform: ExternalPlatform, api_app_id: Optional[str]) -> None:
    """Record the Slack app id on a platform that predates it.

    Only called once the transport has authenticated the event for this
    platform, so the id is trustworthy. Best-effort.
    """
    if not api_app_id:
        return
    config = platform.platform_config or {}
    stored = config.get("app_id")
    if stored == api_app_id:
        return
    if stored:
        logger.warning(
            "SLACK: platform %s is recorded as app %s but received an event for app %s",
            platform.id, stored, api_app_id,
        )
        return
    try:
        platform.platform_config = {**config, "app_id": api_app_id}
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("SLACK: could not record app id: %s", e)


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


async def handle_slack_event(
    db: AsyncSession,
    event_data: Dict[str, Any],
    platform_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Process one parsed Slack ``event_callback`` payload.

    ``platform_id`` is the platform the transport authenticated the event
    for: the Socket Mode listener's own platform, or the one whose signing
    secret verified the webhook. When given it is authoritative — the event
    is never re-attributed by workspace id, which several organizations can
    share. Without it the workspace must identify exactly one platform.

    Returns a small status dict; never raises for routine skips. The caller
    (webhook route / socket client) has already acked transport-level
    concerns (URL challenge, envelope ack, signature).
    """
    team_id = event_data.get("team_id")
    if platform_id:
        platform = await _load_slack_platform(db, platform_id)
    else:
        if not team_id:
            return {"ok": False, "error": "No team_id in event"}
        platform = await find_platform_by_team_id(db, team_id)
    if not platform or not platform.is_active:
        logger.info("SLACK: no active platform for team_id %s", team_id)
        return {"ok": True, "skipped": "no_platform"}

    if platform_id:
        await _remember_app_id(db, platform, event_data.get("api_app_id"))

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
        db, "slack", platform.organization_id, event_data,
        platform_id=str(platform.id),
    )
    return {"ok": True, "result": result}
