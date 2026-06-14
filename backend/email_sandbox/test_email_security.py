"""Inbound security gate: spoofing, allowlist, loop suppression."""
from email import message_from_string

from app.services.email import security


def _msg(headers: dict, body: str = "hello") -> "object":
    raw = "".join(f"{k}: {v}\n" for k, v in headers.items()) + "\n" + body
    return message_from_string(raw)


def test_dmarc_pass_from_allowed_domain_is_allowed():
    msg = _msg({
        "From": "Alice <alice@acme.com>",
        "Subject": "Revenue?",
        "Authentication-Results": "mx.acme.com; dmarc=pass (p=reject); dkim=pass; spf=pass",
        "Message-ID": "<a1@acme.com>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=["acme.com"], own_addresses=["analyst@bow.test"])
    assert v.allowed is True
    assert v.from_address == "alice@acme.com"
    assert v.from_domain == "acme.com"
    assert v.dmarc == "pass"


def test_spoofed_dmarc_fail_is_blocked():
    msg = _msg({
        "From": "CEO <ceo@acme.com>",
        "Authentication-Results": "mx.acme.com; dmarc=fail; dkim=fail; spf=fail",
        "Message-ID": "<spoof@evil.test>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=["acme.com"])
    assert v.allowed is False
    assert "auth_failed" in v.reason


def test_dkim_aligned_without_dmarc_is_allowed():
    # DKIM pass + SPF not failing should clear even if DMARC isn't stamped.
    msg = _msg({
        "From": "bob@acme.com",
        "Authentication-Results": "mx; dkim=pass; spf=pass",
        "Message-ID": "<b@acme.com>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=["acme.com"])
    assert v.allowed is True


def test_offlist_domain_blocked_even_if_authentic():
    msg = _msg({
        "From": "mallory@other.com",
        "Authentication-Results": "mx; dmarc=pass",
        "Message-ID": "<m@other.com>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=["acme.com"])
    assert v.allowed is False
    assert "domain_not_allowed" in v.reason


def test_auto_submitted_is_blocked():
    msg = _msg({
        "From": "alice@acme.com",
        "Authentication-Results": "mx; dmarc=pass",
        "Auto-Submitted": "auto-replied",
        "Message-ID": "<auto@acme.com>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=["acme.com"])
    assert v.allowed is False
    assert v.reason == "auto_or_loop"


def test_own_address_is_loop_blocked():
    msg = _msg({
        "From": "analyst@bow.test",
        "Authentication-Results": "mx; dmarc=pass",
        "Message-ID": "<loop@bow.test>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=[], own_addresses=["analyst@bow.test"])
    assert v.allowed is False


def test_mailing_list_headers_blocked():
    msg = _msg({
        "From": "list@acme.com",
        "Authentication-Results": "mx; dmarc=pass",
        "List-Id": "<engineering.acme.com>",
        "Message-ID": "<list@acme.com>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=["acme.com"])
    assert v.allowed is False


def test_no_allowlist_relies_on_auth_only():
    # Empty allowlist == no domain restriction (internal-only mailbox model).
    msg = _msg({
        "From": "anyone@whatever.com",
        "Authentication-Results": "mx; dmarc=pass",
        "Message-ID": "<x@whatever.com>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=[])
    assert v.allowed is True


def test_metadata_snapshot_is_audit_friendly():
    msg = _msg({
        "From": "alice@acme.com",
        "Authentication-Results": "mx; dmarc=pass; dkim=pass; spf=pass",
        "Message-ID": "<a@acme.com>",
    })
    v = security.evaluate_inbound(msg, allowed_domains=["acme.com"])
    md = v.as_metadata()
    assert md["from_address"] == "alice@acme.com"
    assert md["dmarc"] == "pass"
    assert md["allowed"] is True
