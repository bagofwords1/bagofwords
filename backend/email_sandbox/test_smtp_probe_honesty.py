"""The SMTP test must fail whenever a real send would fail.

Every case here passed the old connect-and-auth probe — it reported
"Connection OK" — while the configuration could not deliver a single message.
The probe is now a real send through the production path, so each of these
must come back as a failure, with the stage that explains it.

These run against a live local relay (``relay.run_relay``) with only
``aiosmtplib``/``aiosmtpd``, no app stack.
"""
from __future__ import annotations

import pytest
from relay import run_relay

from app.services.email.message_builder import build_email
from app.services.email.sender import (
    STAGE_AUTH,
    STAGE_CONFIG,
    STAGE_CONNECT,
    STAGE_RECIPIENT,
    STAGE_SENDER,
    SmtpConfig,
    send_message_result,
)

SENDER = "noreply@acme.com"
RCPT = "admin@acme.com"


def _cfg(relay, **over) -> SmtpConfig:
    base = {
        "host": relay.host, "port": relay.port, "security": "none",
        "username": None, "password": None, "validate_certs": False,
    }
    base.update(over)
    return SmtpConfig(**base)


def _msg(to=RCPT, frm=SENDER):
    return build_email(
        from_address=frm, from_name="Acme", to_address=to,
        subject="probe", body="hello", body_subtype="plain",
    )


async def test_happy_path_delivers():
    with run_relay() as relay:
        ok, error, stage = await send_message_result(_cfg(relay), _msg())
        assert ok, error
        assert relay.count == 1
        assert relay.handler.senders[-1] == SENDER
        assert relay.handler.rcpts[-1] == [RCPT]


async def test_wrong_password_fails_at_auth():
    """The old probe skipped AUTH whenever the password was falsy or wrong."""
    with run_relay(require_auth=("relay-user", "correct-horse")) as relay:
        ok, error, stage = await send_message_result(
            _cfg(relay, username="relay-user", password="wrong"), _msg()
        )
        assert not ok
        assert stage == STAGE_AUTH, f"expected auth failure, got {stage}: {error}"
        assert relay.count == 0


async def test_missing_password_fails_at_auth():
    """A username saved with no password — e.g. a password that would not decrypt.

    This is the exact shape of a Fernet key rotation: ``decrypt_secret`` returns
    ``None``, the sender offers no credentials, and the relay refuses. The old
    probe's ``if username and password`` guard skipped AUTH entirely here and
    reported success.
    """
    with run_relay(require_auth=("relay-user", "correct-horse")) as relay:
        ok, error, stage = await send_message_result(
            _cfg(relay, username="relay-user", password=None), _msg()
        )
        assert not ok
        assert stage == STAGE_AUTH, f"expected auth failure, got {stage}: {error}"
        assert relay.count == 0


async def test_correct_password_authenticates():
    with run_relay(require_auth=("relay-user", "correct-horse")) as relay:
        ok, error, stage = await send_message_result(
            _cfg(relay, username="relay-user", password="correct-horse"), _msg()
        )
        assert ok, f"{stage}: {error}"
        assert relay.count == 1


async def test_unauthorized_sender_is_refused():
    """A relay that will not vouch for the From address — invisible to a probe."""
    with run_relay(allowed_senders=[SENDER]) as relay:
        ok, error, stage = await send_message_result(
            _cfg(relay), _msg(frm="spoofed@elsewhere.test")
        )
        assert not ok
        assert stage == STAGE_SENDER, f"expected sender refusal, got {stage}: {error}"
        assert relay.count == 0


async def test_relay_denied_for_recipient():
    with run_relay(allowed_rcpts=[RCPT]) as relay:
        ok, error, stage = await send_message_result(
            _cfg(relay), _msg(to="outsider@elsewhere.test")
        )
        assert not ok
        assert stage == STAGE_RECIPIENT, f"expected relay denial, got {stage}: {error}"
        assert relay.count == 0


async def test_missing_from_address_is_caught_before_the_wire():
    """No From address is a guaranteed rejection; say so, don't emit a stack trace."""
    with run_relay() as relay:
        with pytest.raises(ValueError):
            build_email(
                from_address=None, from_name=None, to_address=RCPT,
                subject="probe", body="hello",
            )
        # And a hand-rolled message with no From is refused before connecting.
        from email.message import EmailMessage

        bare = EmailMessage()
        bare["To"] = RCPT
        bare.set_content("hello")
        ok, error, stage = await send_message_result(_cfg(relay), bare)
        assert not ok
        assert stage == STAGE_CONFIG
        assert relay.count == 0


async def test_unreachable_host_fails_at_connect():
    from relay import free_port

    dead = free_port()  # nothing is listening here
    cfg = SmtpConfig(host="127.0.0.1", port=dead, security="none", validate_certs=False)
    ok, error, stage = await send_message_result(cfg, _msg())
    assert not ok
    assert stage == STAGE_CONNECT, f"expected connect failure, got {stage}: {error}"


async def test_starttls_required_relay_rejects_plaintext():
    """``security: none`` against a relay that demands STARTTLS must fail loudly."""
    from app.services.email.sender import STAGE_TLS

    with run_relay(require_tls=True) as relay:
        ok, error, stage = await send_message_result(_cfg(relay), _msg())
        assert not ok
        assert relay.count == 0
        assert stage == STAGE_TLS, f"expected tls failure, got {stage}: {error}"


async def test_error_text_is_readable():
    """Admins see the server's own words, not a Python tuple repr."""
    with run_relay(require_auth=("relay-user", "correct-horse")) as relay:
        ok, error, _stage = await send_message_result(
            _cfg(relay, username="relay-user", password="wrong"), _msg()
        )
        assert not ok
        assert not error.startswith("("), f"raw tuple leaked to the UI: {error}"
        assert "535" in error or "Authentication" in error


async def test_sandbox_override_is_not_silently_applied(monkeypatch):
    """``BOW_EMAIL_SMTP_OVERRIDE_HOST`` must not make a bad config look healthy.

    The override exists for sandboxes, but it rewrites *whatever* host is
    configured. A test run that leaves it set proves nothing about the host the
    admin actually typed — so the sandbox suite asserts the override's effect
    explicitly instead of relying on it.
    """
    with run_relay() as sink:
        monkeypatch.setenv("BOW_EMAIL_SMTP_OVERRIDE_HOST", sink.host)
        monkeypatch.setenv("BOW_EMAIL_SMTP_OVERRIDE_PORT", str(sink.port))
        bogus = SmtpConfig(host="nonexistent.invalid", port=25, security="none")
        ok, error, stage = await send_message_result(bogus, _msg())
        # With the override set, a nonsense host "succeeds" — which is exactly
        # why the e2e run must not set it.
        assert ok, f"{stage}: {error}"
        assert sink.count == 1
