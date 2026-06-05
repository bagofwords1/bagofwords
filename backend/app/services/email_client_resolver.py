"""Per-organization outbound email resolution.

When an org configures the Email integration with SMTP, that mailbox becomes
the authoritative outbound transport for *all* of the org's mail (share
notifications, scheduled-report results, verification, and the analyst's own
replies) — overriding the global ``settings.email_client``. If no integration
is configured, we fall back to the global client (existing behavior).

The selection rule is isolated in :func:`choose_outbound` so it is unit-tested
without a DB; :func:`resolve_outbound` is the DB-backed wrapper used by the
notification service.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.services.email.sender import SmtpConfig

logger = logging.getLogger(__name__)


@dataclass
class ResolvedOutbound:
    """How to send mail for a given org."""

    # "integration" -> use the org mailbox via SmtpConfig; "global" -> fastapi-mail
    source: str
    smtp_config: Optional[SmtpConfig] = None
    from_address: Optional[str] = None
    from_name: Optional[str] = None

    @property
    def uses_integration(self) -> bool:
        return self.source == "integration"


def choose_outbound(
    email_platform_config: Optional[dict],
    email_platform_credentials: Optional[dict],
    *,
    global_client_present: bool,
) -> ResolvedOutbound:
    """Pure decision: integration SMTP wins when present, else global fallback.

    ``email_platform_config`` / ``email_platform_credentials`` are the active
    email integration's stored config + decrypted credentials, or ``None`` if
    the org has no email integration.
    """
    creds = email_platform_credentials or {}
    cfg = email_platform_config or {}
    smtp_host = creds.get("smtp_host") or cfg.get("smtp_host")

    if smtp_host:
        smtp = SmtpConfig.from_credentials(creds, cfg)
        from_address = (
            creds.get("from_address")
            or cfg.get("from_address")
            or creds.get("smtp_username")
        )
        return ResolvedOutbound(
            source="integration",
            smtp_config=smtp,
            from_address=from_address,
            from_name=cfg.get("from_name"),
        )

    return ResolvedOutbound(source="global" if global_client_present else "none")


async def resolve_outbound(db, organization_id: str) -> ResolvedOutbound:
    """DB-backed resolution for ``organization_id``."""
    from sqlalchemy import select
    from app.models.external_platform import ExternalPlatform
    from app.settings.config import settings

    platform = None
    if organization_id:
        stmt = select(ExternalPlatform).where(
            ExternalPlatform.organization_id == organization_id,
            ExternalPlatform.platform_type == "email",
            ExternalPlatform.is_active == True,  # noqa: E712
        )
        result = await db.execute(stmt)
        platform = result.scalar_one_or_none()

    config = platform.platform_config if platform else None
    creds = platform.decrypt_credentials() if platform else None
    return choose_outbound(
        config, creds, global_client_present=settings.email_client is not None
    )
