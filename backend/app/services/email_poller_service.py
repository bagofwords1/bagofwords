"""Inbound email poller.

Email has no native webhook, so for integrations with RECEIVE capability we
poll the analyst mailbox and feed new messages into the same
``ExternalPlatformManager.handle_incoming_message`` path that Slack/Teams use.

The loop is dependency-injected (``handler`` + ``reader``) so it can be driven
in tests / the sandbox loopback with a ``FakeMailboxReader`` and a stub handler
— no live IMAP server or DB required. Production wiring is in
:func:`run_email_pollers`, started from ``main.py`` under the scheduler leader.

Security: every message is run through ``security.evaluate_inbound`` *before*
it reaches the agent. Spoofed (DMARC/DKIM-failing), off-allowlist, and
auto/loop messages are dropped here. Identity (existing-member-only, no
auto-provision) is enforced downstream in the manager's email branch.
"""
from __future__ import annotations

import asyncio
import logging
from email import message_from_bytes
from typing import Awaitable, Callable, List, Optional

from app.services.email import security
from app.services.email.mailbox_reader import MailboxReader

logger = logging.getLogger(__name__)

# Process-wide dedup of Message-IDs we've already handled (mirrors the
# slack_webhook in-memory set). Bounded to avoid unbounded growth.
_processed_message_ids: set[str] = set()


def _remember(message_id: str) -> bool:
    """Return True if newly seen, False if already processed."""
    if not message_id:
        return True
    if message_id in _processed_message_ids:
        return False
    _processed_message_ids.add(message_id)
    if len(_processed_message_ids) > 5000:
        _processed_message_ids.clear()
    return True


class EmailPoller:
    """Polls one mailbox and routes authentic messages to ``handler``."""

    def __init__(
        self,
        reader: MailboxReader,
        handler: Callable[[dict], Awaitable[None]],
        *,
        allowed_domains: Optional[List[str]] = None,
        own_addresses: Optional[List[str]] = None,
        require_auth_pass: bool = True,
        on_blocked: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> None:
        self.reader = reader
        self.handler = handler
        self.allowed_domains = allowed_domains or []
        self.own_addresses = own_addresses or []
        self.require_auth_pass = require_auth_pass
        self.on_blocked = on_blocked

    async def poll_once(self) -> dict:
        """Fetch unseen mail, route what's allowed. Returns a summary dict."""
        summary = {"fetched": 0, "routed": 0, "blocked": 0, "skipped": 0}
        messages = await self.reader.fetch_unseen()
        summary["fetched"] = len(messages)
        for uid, raw in messages:
            try:
                msg = message_from_bytes(raw if isinstance(raw, bytes) else raw.encode())
                message_id = (msg.get("Message-ID") or "").strip()
                if not _remember(message_id):
                    summary["skipped"] += 1
                    await self.reader.mark_seen(uid)
                    continue

                verdict = security.evaluate_inbound(
                    msg,
                    allowed_domains=self.allowed_domains,
                    own_addresses=self.own_addresses,
                    require_auth_pass=self.require_auth_pass,
                )
                if not verdict.allowed:
                    summary["blocked"] += 1
                    logger.info(
                        "EMAIL_POLLER: blocked message from %s: %s",
                        verdict.from_address,
                        verdict.reason,
                    )
                    if self.on_blocked:
                        await self.on_blocked(verdict.from_address, verdict.reason)
                    await self.reader.mark_seen(uid)
                    continue

                await self.handler(
                    {
                        "raw": raw,
                        "uid": uid,
                        "security": verdict.as_metadata(),
                    }
                )
                summary["routed"] += 1
            except Exception as e:  # noqa: BLE001 — one bad message must not stall the loop
                logger.warning("EMAIL_POLLER: error handling uid %s: %s", uid, e)
            finally:
                await self.reader.mark_seen(uid)
        return summary

    async def run_forever(self, interval_seconds: float = 30.0, stop_event: Optional[asyncio.Event] = None) -> None:
        while not (stop_event and stop_event.is_set()):
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("EMAIL_POLLER: poll cycle failed: %s", e)
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                    timeout=interval_seconds,
                )
            except asyncio.TimeoutError:
                pass


async def run_email_pollers(interval_seconds: float = 30.0, stop_event: Optional[asyncio.Event] = None) -> None:
    """Production entry point: discover active email channels and poll them.

    Lazily imports the heavy app modules so importing this module stays cheap
    (and unit-testable). Each active ``email`` platform that has IMAP creds and
    ``inbound_enabled`` becomes a polled channel.
    """
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.external_platform import ExternalPlatform
    from app.services.email.mailbox_reader import ImapConfig, ImapMailboxReader
    from app.services.external_platform_manager import ExternalPlatformManager

    manager = ExternalPlatformManager()

    while not (stop_event and stop_event.is_set()):
        try:
            async with async_session_maker() as db:
                stmt = select(ExternalPlatform).where(
                    ExternalPlatform.platform_type == "email",
                    ExternalPlatform.is_active == True,  # noqa: E712
                )
                result = await db.execute(stmt)
                platforms = result.scalars().all()

            for platform in platforms:
                cfg = platform.platform_config or {}
                if not cfg.get("inbound_enabled"):
                    continue
                creds = platform.decrypt_credentials()
                imap_cfg = ImapConfig.from_credentials(creds, cfg)
                if not imap_cfg.host:
                    continue
                from_address = cfg.get("from_address") or creds.get("smtp_username")
                org_id = platform.organization_id

                async def _handler(event_data: dict, _org_id=org_id) -> None:
                    async with async_session_maker() as handler_db:
                        await manager.handle_incoming_message(
                            handler_db, "email", _org_id, event_data
                        )

                poller = EmailPoller(
                    reader=ImapMailboxReader(imap_cfg),
                    handler=_handler,
                    allowed_domains=cfg.get("allowed_domains") or [],
                    own_addresses=[from_address] if from_address else [],
                    require_auth_pass=bool(cfg.get("require_auth_pass", True)),
                )
                await poller.poll_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("EMAIL_POLLER: discovery cycle failed: %s", e)

        try:
            await asyncio.wait_for(
                stop_event.wait() if stop_event else asyncio.sleep(interval_seconds),
                timeout=interval_seconds,
            )
        except asyncio.TimeoutError:
            pass
