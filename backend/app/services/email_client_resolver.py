"""Per-organization outbound email resolution (purpose-aware).

Two kinds of outbound mail use different transports:

- **analyst** — the agent ``send_email`` tool + channel replies. Always the
  **AI mailbox** (the Email integration), so the From identity and reply
  threading hold. Falls back to org-SMTP/global only if no AI mailbox exists.
- **system** — shares, scheduled reports, verification/registration links, any
  notification. Precedence: **Org SMTP** (``OrganizationSettings.config.smtp``)
  → **Global SMTP** (``settings.email_client`` from bow-config). **Never** the
  AI mailbox.

Backward/forward compatible:
- Org with no DB SMTP → falls through to the global bow-config client.
- Org that sets DB SMTP → uses it even when the global bow-config SMTP is empty.

The decision is isolated in :func:`choose_outbound` (pure, unit-tested);
:func:`resolve_outbound` is the DB-backed wrapper.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.services.email.sender import SmtpConfig

logger = logging.getLogger(__name__)


@dataclass
class ResolvedOutbound:
    """How to send mail for a given org + purpose."""

    # "ai_mailbox" | "org_smtp" | "global" | "none"
    source: str
    smtp_config: Optional[SmtpConfig] = None
    from_address: Optional[str] = None
    from_name: Optional[str] = None

    @property
    def uses_smtp_config(self) -> bool:
        """True when we should send via build_email + sender (not fastapi-mail)."""
        return self.source in ("ai_mailbox", "org_smtp")

    @property
    def uses_global(self) -> bool:
        return self.source == "global"

    # Back-compat alias (old callers checked uses_integration).
    @property
    def uses_integration(self) -> bool:
        return self.uses_smtp_config


def _ai_mailbox_resolved(ai_config: Optional[dict], ai_creds: Optional[dict]) -> Optional[ResolvedOutbound]:
    creds = ai_creds or {}
    cfg = ai_config or {}
    host = creds.get("smtp_host") or cfg.get("smtp_host")
    if not host:
        return None
    return ResolvedOutbound(
        source="ai_mailbox",
        smtp_config=SmtpConfig.from_credentials(creds, cfg),
        from_address=(creds.get("from_address") or cfg.get("from_address") or creds.get("smtp_username")),
        from_name=cfg.get("from_name"),
    )


def _org_smtp_resolved(org_smtp: Optional[dict]) -> Optional[ResolvedOutbound]:
    if not (org_smtp and org_smtp.get("enabled") and org_smtp.get("host")):
        return None
    return ResolvedOutbound(
        source="org_smtp",
        smtp_config=SmtpConfig(
            host=org_smtp["host"],
            port=int(org_smtp.get("port") or 587),
            username=org_smtp.get("username"),
            password=org_smtp.get("password"),
            security=org_smtp.get("security") or "starttls",
            validate_certs=bool(org_smtp.get("validate_certs", True)),
        ),
        from_address=(org_smtp.get("from_address") or org_smtp.get("username")),
        from_name=org_smtp.get("from_name"),
    )


def choose_outbound(
    purpose: str,
    ai_config: Optional[dict],
    ai_creds: Optional[dict],
    org_smtp: Optional[dict],
    *,
    global_present: bool,
) -> ResolvedOutbound:
    """Pure resolution. ``purpose`` is "analyst" or "system".

    ``org_smtp`` is the org's decrypted SMTP dict
    ``{enabled, host, port, security, username, password, from_address, from_name}``
    or ``None``.
    """
    if purpose == "analyst":
        ai = _ai_mailbox_resolved(ai_config, ai_creds)
        if ai:
            return ai
        # No AI mailbox configured — analyst mail still needs to go out, so use
        # the system precedence (org SMTP → global).

    org = _org_smtp_resolved(org_smtp)
    if org:
        return org
    return ResolvedOutbound(source="global" if global_present else "none")


async def get_org_smtp(db, organization_id: str) -> Optional[dict]:
    """Load + decrypt the org's SMTP settings from OrganizationSettings.config.smtp."""
    if not organization_id:
        return None
    from sqlalchemy import select
    from app.models.organization_settings import OrganizationSettings
    from app.services.email.secrets import decrypt_secret

    stmt = select(OrganizationSettings).where(
        OrganizationSettings.organization_id == organization_id
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row or not isinstance(row.config, dict):
        return None
    smtp = row.config.get("smtp")
    if not isinstance(smtp, dict):
        return None
    out = dict(smtp)
    out["password"] = decrypt_secret(smtp.get("password_enc"))
    return out


async def resolve_outbound(db, organization_id: str, purpose: str = "system") -> ResolvedOutbound:
    """DB-backed resolution for ``organization_id`` + ``purpose``."""
    from sqlalchemy import select
    from app.models.external_platform import ExternalPlatform
    from app.settings.config import settings

    ai_config = ai_creds = None
    if purpose == "analyst" and organization_id:
        stmt = select(ExternalPlatform).where(
            ExternalPlatform.organization_id == organization_id,
            ExternalPlatform.platform_type == "email",
            ExternalPlatform.is_active == True,  # noqa: E712
        )
        result = await db.execute(stmt)
        platform = result.scalar_one_or_none()
        if platform:
            ai_config = platform.platform_config
            ai_creds = platform.decrypt_credentials()

    org_smtp = await get_org_smtp(db, organization_id)
    return choose_outbound(
        purpose, ai_config, ai_creds, org_smtp,
        global_present=settings.email_client is not None,
    )
