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
from typing import Optional, Tuple

import aiosmtplib

from app.services.email.oauth import OAuthSettings, get_xoauth2_string

logger = logging.getLogger(__name__)


# Which step of the SMTP conversation an error came from. Surfaced by the
# settings "send test email" probe so an admin sees *what* the relay rejected
# ("auth", "sender") instead of a bare transcript line.
STAGE_CONFIG = "config"
STAGE_CONNECT = "connect"
STAGE_TLS = "tls"
STAGE_AUTH = "auth"
STAGE_SENDER = "sender"
STAGE_RECIPIENT = "recipient"
STAGE_SEND = "send"


def _err(name: str):
    """aiosmtplib error class by name, or a never-matching sentinel.

    Looked up defensively so a rename across aiosmtplib majors degrades the
    *classification* of a failure rather than raising inside the error handler.
    """
    return getattr(aiosmtplib, name, None) or getattr(
        getattr(aiosmtplib, "errors", None), name, None
    ) or type("_Missing", (Exception,), {})


def _response_text(exc: BaseException) -> str:
    """The server's human-readable response text, if the exception carries one."""
    message = getattr(exc, "message", None)
    if isinstance(message, bytes):
        message = message.decode("utf-8", "replace")
    return message if isinstance(message, str) else ""


def classify_error(exc: BaseException) -> str:
    """Map a transport exception to the stage of the SMTP conversation it hit.

    The response *code* is consulted before the exception type, because a relay
    reports "you have not authenticated" against whichever command it happened
    to refuse. A server requiring AUTH answers ``MAIL FROM`` with
    ``530 Authentication required``, which arrives as ``SMTPSenderRefused`` —
    reading only the type would blame the From address for a password problem.
    """
    if isinstance(exc, _err("SMTPAuthenticationError")):
        return STAGE_AUTH

    code = getattr(exc, "code", None)
    if code == 530:
        # 530 is overloaded: "must issue a STARTTLS command first" and
        # "authentication required" share it. The text is the only separator.
        text = _response_text(exc).lower()
        return STAGE_TLS if "tls" in text else STAGE_AUTH
    if code in (534, 535, 538):
        return STAGE_AUTH

    if isinstance(exc, _err("SMTPSenderRefused")):
        return STAGE_SENDER
    if isinstance(exc, (_err("SMTPRecipientsRefused"), _err("SMTPRecipientRefused"))):
        return STAGE_RECIPIENT
    if isinstance(exc, ssl.SSLError):
        return STAGE_TLS
    if isinstance(exc, (_err("SMTPConnectError"), _err("SMTPConnectTimeoutError"))):
        return STAGE_CONNECT
    if isinstance(exc, _err("SMTPServerDisconnected")):
        return STAGE_CONNECT
    return STAGE_SEND


def describe_error(exc: BaseException) -> str:
    """A one-line, admin-readable rendering of a transport failure.

    ``str(exc)`` on an aiosmtplib response error is a raw tuple
    (``(530, '5.7.0 Authentication required', 'noreply@acme.com')``), which is
    noise in a settings page. Prefer the code and the server's own words.
    """
    code = getattr(exc, "code", None)
    text = _response_text(exc)
    if code and text:
        return f"{code} {text}"
    return str(exc) or exc.__class__.__name__


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    username: Optional[str] = None
    password: Optional[str] = None
    # "starttls" | "ssl" | "none"
    security: str = "starttls"
    # "password" | "microsoft" | "google"
    auth_type: str = "password"
    validate_certs: bool = True
    # Present when auth_type is an OAuth provider; carries the token-minting creds.
    oauth: Optional[OAuthSettings] = None

    @classmethod
    def from_credentials(cls, creds: dict, config: Optional[dict] = None) -> "SmtpConfig":
        """Build from a platform's decrypted credentials + non-secret config."""
        creds = creds or {}
        config = config or {}
        auth_type = creds.get("auth_type") or config.get("auth_type") or "password"
        return cls(
            host=creds.get("smtp_host") or config.get("smtp_host"),
            port=int(creds.get("smtp_port") or config.get("smtp_port") or 587),
            username=creds.get("smtp_username") or creds.get("username"),
            password=creds.get("smtp_password") or creds.get("password"),
            security=(creds.get("smtp_security") or config.get("smtp_security") or "starttls"),
            auth_type=auth_type,
            validate_certs=bool(config.get("validate_certs", True)),
            oauth=OAuthSettings.from_credentials(creds, config),
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
            oauth=self.oauth,
        )


def _tls_context(cfg: SmtpConfig):
    if cfg.validate_certs:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _send_xoauth2(cfg: SmtpConfig, msg: EmailMessage) -> bool:
    """Send using an OAuth access token via the XOAUTH2 SASL mechanism.

    Used for Microsoft 365 (app-only) and Google Workspace (service account)
    where Basic Auth is unavailable. We drive the aiosmtplib client manually
    because ``aiosmtplib.send`` only does password AUTH.
    """
    sasl = await get_xoauth2_string(cfg.oauth)
    # Let aiosmtplib negotiate TLS during connect() — passing start_tls here
    # (instead of calling starttls() afterwards) avoids the "Connection already
    # using TLS" error from a double STARTTLS.
    client = aiosmtplib.SMTP(
        hostname=cfg.host,
        port=cfg.port,
        use_tls=(cfg.security == "ssl"),
        start_tls=(cfg.security == "starttls"),
        tls_context=_tls_context(cfg),
        timeout=30,
    )
    await client.connect()
    try:
        # Re-EHLO in the (post-STARTTLS) session; the low-level execute_command
        # below skips aiosmtplib's automatic helo, so do it explicitly or the
        # server answers "503 Send hello first".
        await client.ehlo()
        # AUTH XOAUTH2 <base64 initial response>
        code, message = await client.execute_command(
            b"AUTH", b"XOAUTH2", sasl.encode("ascii")
        )
        if code != 235:
            # On failure servers send a 334 base64 error; send an empty line to
            # surface it, then bail.
            try:
                await client.execute_command(b"")
            except Exception:  # noqa: BLE001
                pass
            logger.warning("EMAIL_SENDER: XOAUTH2 auth failed (%s): %s", code, message)
            return False
        await client.send_message(msg)
        return True
    finally:
        try:
            await client.quit()
        except Exception:  # noqa: BLE001
            pass


async def send_message_result(cfg: SmtpConfig, msg: EmailMessage) -> Tuple[bool, Optional[str], str]:
    """Send ``msg``; return ``(ok, error, stage)``.

    The error-preserving form of :func:`send_message`. Sending mail is the only
    honest test of an SMTP config — a connect-and-auth probe cannot see a relay
    that refuses the envelope sender or declines to relay to the recipient — so
    the settings page drives this and reports the stage that failed.
    """
    cfg = cfg.resolved()
    if not cfg.host:
        logger.warning("EMAIL_SENDER: no SMTP host configured")
        return False, "no SMTP host configured", STAGE_CONFIG
    if not msg.get("From"):
        # build_email refuses to construct this, but a caller assembling its own
        # EmailMessage can still get here; every relay rejects a missing sender.
        return False, "no From address configured", STAGE_CONFIG

    try:
        if cfg.oauth is not None:
            ok = await _send_xoauth2(cfg, msg)
            return ok, (None if ok else "XOAUTH2 authentication failed"), (
                STAGE_SEND if ok else STAGE_AUTH
            )

        use_tls = cfg.security == "ssl"
        start_tls = cfg.security == "starttls"
        kwargs = dict(
            hostname=cfg.host,
            port=cfg.port,
            use_tls=use_tls,
            start_tls=start_tls if start_tls else None,
            timeout=30,
        )
        tls_context = _tls_context(cfg)
        if tls_context is not None:
            kwargs["tls_context"] = tls_context
        if cfg.auth_type == "password" and cfg.username and cfg.password:
            kwargs["username"] = cfg.username
            kwargs["password"] = cfg.password
        await aiosmtplib.send(msg, **kwargs)
        return True, None, STAGE_SEND
    except Exception as e:  # noqa: BLE001 — transport errors must not crash the agent
        stage = classify_error(e)
        detail = describe_error(e)
        logger.warning(
            "EMAIL_SENDER: failed to send to %s via %s:%s at stage=%s: %s",
            msg.get("To"), cfg.host, cfg.port, stage, detail,
        )
        return False, detail, stage


async def send_message(cfg: SmtpConfig, msg: EmailMessage) -> bool:
    """Send ``msg`` via SMTP. Returns True on success, False on failure.

    Bool-returning wrapper kept for the fire-and-forget senders; callers that
    need to report *why* a send failed use :func:`send_message_result`.
    """
    ok, _error, _stage = await send_message_result(cfg, msg)
    return ok
