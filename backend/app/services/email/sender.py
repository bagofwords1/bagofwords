"""Outbound SMTP transport for the email channel.

Thin wrapper over :mod:`aiosmtplib` (already a dependency via fastapi-mail).
The transport is intentionally separate from the message *content* so the
analyst's replies and the org's notification mail can share one sender, and so
a future ``xoauth2`` auth strategy slots in without touching the rest.

Sandbox/tests can redirect all sends to a local SMTP sink by setting
``BOW_EMAIL_SMTP_OVERRIDE_HOST`` / ``BOW_EMAIL_SMTP_OVERRIDE_PORT`` — mirroring
the ``WHATSAPP_GRAPH_BASE_URL`` override used by the WhatsApp adapter.
"""
from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

import aiosmtplib

logger = logging.getLogger(__name__)


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    username: Optional[str] = None
    password: Optional[str] = None
    # "starttls" | "ssl" | "none"
    security: str = "starttls"
    # "password" | "xoauth2"  (v1 ships "password"; xoauth2 is a future strategy)
    auth_type: str = "password"
    validate_certs: bool = True

    @classmethod
    def from_credentials(cls, creds: dict, config: Optional[dict] = None) -> "SmtpConfig":
        """Build from a platform's decrypted credentials + non-secret config."""
        creds = creds or {}
        config = config or {}
        return cls(
            host=creds.get("smtp_host") or config.get("smtp_host"),
            port=int(creds.get("smtp_port") or config.get("smtp_port") or 587),
            username=creds.get("smtp_username") or creds.get("username"),
            password=creds.get("smtp_password") or creds.get("password"),
            security=(creds.get("smtp_security") or config.get("smtp_security") or "starttls"),
            auth_type=(creds.get("auth_type") or config.get("auth_type") or "password"),
            validate_certs=bool(config.get("validate_certs", True)),
        )

    def resolved(self) -> "SmtpConfig":
        """Apply sandbox host/port overrides if present (returns self otherwise)."""
        host = os.environ.get("BOW_EMAIL_SMTP_OVERRIDE_HOST")
        if not host:
            return self
        port = int(os.environ.get("BOW_EMAIL_SMTP_OVERRIDE_PORT", "0")) or self.port
        # Overridden sinks are local/plaintext.
        return SmtpConfig(
            host=host,
            port=port,
            username=None,
            password=None,
            security="none",
            auth_type=self.auth_type,
            validate_certs=False,
        )


async def send_message(cfg: SmtpConfig, msg: EmailMessage) -> bool:
    """Send ``msg`` via SMTP. Returns True on success, False on failure."""
    cfg = cfg.resolved()
    if not cfg.host:
        logger.warning("EMAIL_SENDER: no SMTP host configured")
        return False

    use_tls = cfg.security == "ssl"
    start_tls = cfg.security == "starttls"

    tls_context = None
    if (use_tls or start_tls) and not cfg.validate_certs:
        tls_context = ssl.create_default_context()
        tls_context.check_hostname = False
        tls_context.verify_mode = ssl.CERT_NONE

    kwargs = dict(
        hostname=cfg.host,
        port=cfg.port,
        use_tls=use_tls,
        start_tls=start_tls if start_tls else None,
        timeout=30,
    )
    if tls_context is not None:
        kwargs["tls_context"] = tls_context
    if cfg.auth_type == "password" and cfg.username and cfg.password:
        kwargs["username"] = cfg.username
        kwargs["password"] = cfg.password

    try:
        await aiosmtplib.send(msg, **kwargs)
        return True
    except Exception as e:  # noqa: BLE001 — transport errors must not crash the agent
        logger.warning("EMAIL_SENDER: failed to send to %s: %s", msg.get("To"), e)
        return False
