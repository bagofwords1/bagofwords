"""Deliver a completion result to a user on a channel (Teams, Slack, AI mailbox, plain SMTP).

Resolution: a channel is usable only if the org has it enabled AND (for chat
platforms) the user has a verified identity mapping. Unavailable channels fall
back to email, then to skip. Plain SMTP is rendered as plain, human-sounding text
with a "continue this discussion" link (it has no inbound reply path); AI mailbox
and chat platforms support replies that continue the report.

A mock mode (env ``BOW_CHANNELS_MOCK=1``) records deliveries to an outbox file
instead of calling real APIs, so the feature can be verified end-to-end (incl.
"sent to a mock Teams channel") without live credentials.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping
from app.models.user import User
from app.models.organization import Organization

logger = logging.getLogger(__name__)

# Channels that ride on a connected chat platform (need a verified user mapping).
CHAT_CHANNELS = {"teams", "slack", "whatsapp"}
# Channels that go out over email transports.
EMAIL_CHANNELS = {"ai_mailbox", "smtp"}
ALL_CHANNELS = CHAT_CHANNELS | EMAIL_CHANNELS

_EMAIL_PURPOSE = {"ai_mailbox": "analyst", "smtp": "system"}


def _mock_enabled() -> bool:
    return os.environ.get("BOW_CHANNELS_MOCK", "").strip() in ("1", "true", "True", "yes")


def _mock_outbox_path() -> str:
    return os.environ.get("BOW_CHANNELS_MOCK_FILE", "/tmp/bow_channel_outbox.json")


def record_mock_delivery(entry: dict) -> None:
    """Append a delivery to the mock outbox file (best-effort)."""
    entry = {**entry, "ts": datetime.utcnow().isoformat()}
    path = _mock_outbox_path()
    try:
        existing: List[dict] = []
        if os.path.exists(path):
            with open(path, "r") as f:
                existing = json.load(f) or []
        existing.append(entry)
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to record mock delivery: %s", e)


def read_mock_outbox() -> List[dict]:
    path = _mock_outbox_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f) or []
    except Exception:
        return []


def clear_mock_outbox() -> None:
    path = _mock_outbox_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


@dataclass
class DeliveryResult:
    requested_channel: Optional[str]
    used_channel: Optional[str]          # what we actually sent on (may differ via fallback)
    status: str                          # 'sent' | 'skipped' | 'failed'
    mock: bool = False
    detail: Optional[str] = None
    external_user_id: Optional[str] = None
    thread_ref: Optional[str] = None


class ChannelDeliveryService:

    # ───────────────────────── resolution ─────────────────────────

    async def _get_platform(self, db: AsyncSession, org_id: str, platform_type: str) -> Optional[ExternalPlatform]:
        rows = await db.execute(
            select(ExternalPlatform)
            .filter(ExternalPlatform.organization_id == org_id)
            .filter(ExternalPlatform.platform_type == platform_type)
            .filter(ExternalPlatform.is_active == True)
            .filter(ExternalPlatform.deleted_at == None)
        )
        return rows.scalars().first()

    async def _get_user_mapping(self, db, org_id, platform_type, user_id) -> Optional[ExternalUserMapping]:
        rows = await db.execute(
            select(ExternalUserMapping)
            .filter(ExternalUserMapping.organization_id == org_id)
            .filter(ExternalUserMapping.platform_type == platform_type)
            .filter(ExternalUserMapping.app_user_id == user_id)
            .filter(ExternalUserMapping.is_verified == True)
            .filter(ExternalUserMapping.deleted_at == None)
        )
        return rows.scalars().first()

    async def _email_available(self, db, org_id: str, purpose: str) -> bool:
        try:
            from app.services.email_client_resolver import is_outbound_available
            return await is_outbound_available(db, org_id, purpose=purpose)
        except Exception:
            # global SMTP fallback
            try:
                from app.settings.config import settings
                return settings.email_client is not None
            except Exception:
                return False

    async def channel_available(self, db, org: Organization, user: User, channel: str) -> bool:
        """Is `channel` usable for this user right now?"""
        if _mock_enabled():
            return True  # mock mode pretends everything is wired
        if channel in EMAIL_CHANNELS:
            return await self._email_available(db, str(org.id), _EMAIL_PURPOSE[channel])
        if channel in CHAT_CHANNELS:
            plat = await self._get_platform(db, str(org.id), channel)
            if not plat:
                return False
            mapping = await self._get_user_mapping(db, str(org.id), channel, str(user.id))
            return mapping is not None
        return False

    # ───────────────────────── rendering ─────────────────────────

    @staticmethod
    def _plain_smtp_body(user: User, title: str, content: str, report_url: Optional[str]) -> str:
        """Plain-text, human-sounding email (no HTML template). SMTP has no reply
        path, so we point the reader back to the report to continue."""
        name = (getattr(user, "name", None) or "there").split(" ")[0]
        lines = [f"Hi {name},", ""]
        if title:
            lines.append(f"Here's your update on \"{title}\":")
            lines.append("")
        lines.append((content or "").strip() or "(no output)")
        lines.append("")
        if report_url:
            lines.append(f"You can continue this discussion here: {report_url}")
            lines.append("")
        lines.append("— Your analyst")
        return "\n".join(lines)

    @staticmethod
    def _ai_mailbox_body(user: User, title: str, content: str, report_url: Optional[str]) -> str:
        body = ChannelDeliveryService._plain_smtp_body(user, title, content, report_url)
        return body  # reply-by-email continues the report; link is still handy

    @staticmethod
    def _chat_body(title: str, content: str) -> str:
        prefix = f"*{title}*\n\n" if title else ""
        return f"{prefix}{(content or '').strip() or '(no output)'}"

    # ───────────────────────── delivery ─────────────────────────

    async def deliver(
        self, db: AsyncSession, organization: Organization, user: User,
        channel: Optional[str], *, title: str, content: str, report_url: Optional[str] = None,
        report_id: Optional[str] = None,
    ) -> DeliveryResult:
        if not channel:
            return DeliveryResult(requested_channel=None, used_channel=None, status="skipped",
                                  detail="no channel configured")
        if channel not in ALL_CHANNELS:
            return DeliveryResult(requested_channel=channel, used_channel=None, status="skipped",
                                  detail=f"unknown channel {channel}")

        # Resolve with fallback: requested → email → skip.
        used = channel
        if not await self.channel_available(db, organization, user, channel):
            # fall back to whichever email transport is available
            fallback = None
            for em in ("ai_mailbox", "smtp"):
                if em != channel and await self.channel_available(db, organization, user, em):
                    fallback = em
                    break
            if fallback is None:
                logger.info("Channel %s unavailable for user %s; skipping", channel, user.id)
                return DeliveryResult(requested_channel=channel, used_channel=None, status="skipped",
                                      detail="channel unavailable, no email fallback")
            used = fallback

        # ── mock mode: record instead of sending ──
        if _mock_enabled():
            ext_user_id = None
            if used in CHAT_CHANNELS:
                mapping = await self._get_user_mapping(db, str(organization.id), used, str(user.id))
                ext_user_id = mapping.external_user_id if mapping else f"mock-{used}-{user.id}"
            body = (self._chat_body(title, content) if used in CHAT_CHANNELS
                    else self._plain_smtp_body(user, title, content, report_url))
            record_mock_delivery({
                "channel": used, "requested_channel": channel,
                "recipient_user_id": str(user.id), "recipient_email": getattr(user, "email", None),
                "external_user_id": ext_user_id, "title": title, "body": body,
                "report_id": report_id, "report_url": report_url,
                "organization_id": str(organization.id),
            })
            return DeliveryResult(requested_channel=channel, used_channel=used, status="sent",
                                  mock=True, external_user_id=ext_user_id)

        # ── real delivery ──
        try:
            if used in EMAIL_CHANNELS:
                return await self._send_email(db, organization, user, used, title, content, report_url, channel)
            return await self._send_chat(db, organization, user, used, title, content, report_id, channel)
        except Exception as e:  # noqa: BLE001
            logger.error("Delivery on %s failed for user %s: %s", used, user.id, e)
            return DeliveryResult(requested_channel=channel, used_channel=used, status="failed", detail=str(e))

    async def _send_email(self, db, organization, user, channel, title, content, report_url, requested) -> DeliveryResult:
        from app.services.notification_service import notification_service
        if not getattr(user, "email", None):
            return DeliveryResult(requested_channel=requested, used_channel=channel, status="skipped",
                                  detail="user has no email")
        body = (self._ai_mailbox_body if channel == "ai_mailbox" else self._plain_smtp_body)(
            user, title, content, report_url)
        subject = title or "Your scheduled update"
        res = await notification_service.send_custom_email(
            recipients=[user.email], subject=subject, body=body, subtype="plain",
            db=db, organization_id=str(organization.id), purpose=_EMAIL_PURPOSE[channel],
        )
        status = "sent" if getattr(res, "status", "") == "sent" else "failed"
        return DeliveryResult(requested_channel=requested, used_channel=channel, status=status,
                              detail=getattr(res, "error", None))

    async def _send_chat(self, db, organization, user, channel, title, content, report_id, requested) -> DeliveryResult:
        from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
        plat = await self._get_platform(db, str(organization.id), channel)
        mapping = await self._get_user_mapping(db, str(organization.id), channel, str(user.id))
        if not plat or not mapping:
            return DeliveryResult(requested_channel=requested, used_channel=channel, status="skipped",
                                  detail="platform or user mapping missing")
        adapter = PlatformAdapterFactory.create_adapter(plat)
        body = self._chat_body(title, content)
        ok = await adapter.send_dm_in_thread(user_id=mapping.external_user_id, text=body)
        # Persist platform linkage on the report so a reply can continue it.
        if report_id:
            try:
                from app.models.report import Report
                rep = await db.get(Report, report_id)
                if rep is not None:
                    rep.external_platform_id = plat.id
                    await db.commit()
            except Exception:
                logger.debug("Failed to persist external_platform_id on report %s", report_id, exc_info=True)
        return DeliveryResult(
            requested_channel=requested, used_channel=channel,
            status="sent" if ok else "failed", external_user_id=mapping.external_user_id,
        )


channel_delivery_service = ChannelDeliveryService()
