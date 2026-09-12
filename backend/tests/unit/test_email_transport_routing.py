"""System email must leave through the organization's own SMTP server.

Two properties are locked down here:

1. **No silent fallback.** An organization that configured and enabled its own
   relay is authoritative. When that relay refuses the message we report the
   failure; re-sending through the global bow-config SMTP would deliver the mail
   from an identity the admin never chose and make a broken relay look healthy.

2. **Nothing sends via the global client behind the resolver's back.** The bug
   this suite exists to prevent was six senders reading ``settings.email_client``
   directly, so invites, shares, welcome mail and account mail ignored the org's
   configuration entirely. The guard test fails if that pattern reappears.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.services.email.sender import SmtpConfig
from app.services.email_client_resolver import ResolvedOutbound, choose_outbound
from app.services.notification_service import NotificationService

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"

# The only modules allowed to touch the global fastapi-mail client: the one that
# builds it, the one that decides which transport to use, and the one that owns
# the global send path.
GLOBAL_CLIENT_OWNERS = {
    "app/settings/config.py",
    "app/services/email_client_resolver.py",
    "app/services/notification_service.py",
}


def _org_smtp(**over) -> dict:
    base = {
        "enabled": True, "host": "relay.acme.com", "port": 587,
        "security": "starttls", "username": "noreply@acme.com",
        "password": "s3cret", "from_address": "noreply@acme.com",
        "from_name": "Acme", "validate_certs": True,
    }
    base.update(over)
    return base


class TestNoSilentFallback:
    """A failing org relay must not be papered over by the global client."""

    @pytest.mark.asyncio
    async def test_org_smtp_failure_does_not_reach_global_client(self, monkeypatch):
        service = NotificationService()
        resolved = ResolvedOutbound(
            source="org_smtp",
            smtp_config=SmtpConfig(host="relay.acme.com"),
            from_address="noreply@acme.com",
            from_name="Acme",
        )

        async def _fail(cfg, msg):
            return False, "535 5.7.8 Authentication credentials invalid", "auth"

        global_calls = []

        async def _global(*args, **kwargs):
            global_calls.append(kwargs)
            raise AssertionError("must not fall back to the global SMTP client")

        monkeypatch.setattr(
            "app.services.email.sender.send_message_result", _fail, raising=True
        )
        monkeypatch.setattr(service, "_send_via_global", _global, raising=True)

        outcome = await service._send_via_smtp_config(
            resolved, ["admin@acme.com"], "subject", "body",
            subtype="plain", attachments=None,
            message_id=None, in_reply_to=None, references=None,
            retries=0, retry_delay=0, timeout=None,
        )

        assert outcome.ok is False
        assert outcome.source == "org_smtp"
        assert outcome.stage == "auth"
        assert "535" in (outcome.error or "")
        assert global_calls == []

    @pytest.mark.asyncio
    async def test_missing_from_address_fails_with_the_fix_not_a_stack_trace(self):
        service = NotificationService()
        resolved = ResolvedOutbound(
            source="org_smtp",
            smtp_config=SmtpConfig(host="relay.acme.com"),
            from_address=None,
            from_name=None,
        )
        outcome = await service._send_via_smtp_config(
            resolved, ["admin@acme.com"], "subject", "body",
            subtype="plain", attachments=None,
            message_id=None, in_reply_to=None, references=None,
            retries=0, retry_delay=0, timeout=None,
        )
        assert outcome.ok is False
        assert outcome.stage == "config"
        assert "From address" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_retry_does_not_redeliver_to_accepted_recipients(self, monkeypatch):
        """A partial failure must retry only the recipients that were refused."""
        service = NotificationService()
        resolved = ResolvedOutbound(
            source="org_smtp",
            smtp_config=SmtpConfig(host="relay.acme.com"),
            from_address="noreply@acme.com",
        )
        attempts: list[str] = []

        async def _selective(cfg, msg):
            rcpt = msg["To"]
            attempts.append(rcpt)
            if rcpt == "flaky@acme.com" and attempts.count(rcpt) == 1:
                return False, "451 temporary failure", "send"
            return True, None, "send"

        monkeypatch.setattr(
            "app.services.email.sender.send_message_result", _selective, raising=True
        )

        outcome = await service._send_via_smtp_config(
            resolved, ["ok@acme.com", "flaky@acme.com"], "subject", "body",
            subtype="plain", attachments=None,
            message_id=None, in_reply_to=None, references=None,
            retries=1, retry_delay=0, timeout=None,
        )

        assert outcome.ok is True
        assert attempts.count("ok@acme.com") == 1, "accepted recipient was re-sent"
        assert attempts.count("flaky@acme.com") == 2


class TestResolutionPrecedence:
    def test_enabled_org_smtp_wins_over_global(self):
        resolved = choose_outbound(
            "system", None, None, _org_smtp(), global_present=True
        )
        assert resolved.source == "org_smtp"
        assert resolved.smtp_config.host == "relay.acme.com"

    def test_org_smtp_used_even_without_a_global_client(self):
        resolved = choose_outbound(
            "system", None, None, _org_smtp(), global_present=False
        )
        assert resolved.source == "org_smtp"

    def test_disabled_org_smtp_falls_back_to_global(self):
        resolved = choose_outbound(
            "system", None, None, _org_smtp(enabled=False), global_present=True
        )
        assert resolved.source == "global"

    def test_disabled_org_smtp_without_global_is_none(self):
        resolved = choose_outbound(
            "system", None, None, _org_smtp(enabled=False), global_present=False
        )
        assert resolved.source == "none"

    def test_system_mail_never_uses_the_ai_mailbox(self):
        ai_creds = {"smtp_host": "smtp.office365.com", "smtp_username": "ai@acme.com"}
        resolved = choose_outbound(
            "system", {}, ai_creds, _org_smtp(), global_present=True
        )
        assert resolved.source == "org_smtp"

    def test_from_address_falls_back_to_username(self):
        resolved = choose_outbound(
            "system", None, None, _org_smtp(from_address=None), global_present=True
        )
        assert resolved.from_address == "noreply@acme.com"


class TestNoDirectGlobalClientUse:
    """Nothing may send via ``settings.email_client`` behind the resolver."""

    def test_no_module_reads_the_global_client_directly(self):
        pattern = re.compile(r"^(?!\s*#).*\bemail_client\b", re.MULTILINE)
        offenders = []
        for path in APP_ROOT.rglob("*.py"):
            rel = path.relative_to(BACKEND_ROOT).as_posix()
            if rel in GLOBAL_CLIENT_OWNERS:
                continue
            for match in pattern.finditer(path.read_text()):
                line = match.group(0).strip()
                # Docstrings and prose may name it; only code counts.
                if "settings.email_client" in line or "email_client" in line.split("#")[0]:
                    if line.startswith(('"', "'", "*", "-")) or line.endswith('"""'):
                        continue
                    offenders.append(f"{rel}: {line}")
        assert not offenders, (
            "These modules bypass the org-SMTP resolver and will silently send "
            "via the global bow-config SMTP:\n  " + "\n  ".join(offenders)
            + "\n\nUse notification_service.send_custom_email(db=..., "
              "organization_id=...) or email_client_resolver.is_outbound_available()."
        )

    def test_no_module_builds_its_own_fastapi_mail_message(self):
        """``MessageSchema`` outside the notification service means a bypass."""
        allowed = {"app/services/notification_service.py"}
        offenders = []
        for path in APP_ROOT.rglob("*.py"):
            rel = path.relative_to(BACKEND_ROOT).as_posix()
            if rel in allowed:
                continue
            text = path.read_text()
            if re.search(r"^\s*from fastapi_mail import.*MessageSchema", text, re.MULTILINE):
                offenders.append(rel)
        assert not offenders, (
            "These modules construct fastapi-mail messages directly, which can "
            "only reach the global SMTP client:\n  " + "\n  ".join(offenders)
        )


class TestInviteSenderRequiresOrgContext:
    """The invite sender must demand the org context it needs to resolve SMTP."""

    def test_send_invitation_email_takes_db_and_organization_id(self):
        import inspect

        from app.services.organization_service import OrganizationService

        params = list(
            inspect.signature(OrganizationService._send_invitation_email).parameters
        )
        assert params[:3] == ["self", "db", "organization_id"], (
            "invites resolved SMTP from no organization context and always used "
            f"the global relay; signature is now {params}"
        )
