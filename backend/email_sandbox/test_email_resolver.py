"""Outbound resolution: org Email integration overrides the global SMTP client.

This is the heart of the "if you configure email it becomes the org's SMTP
client and overrides the config" requirement, and of the SMTP-only vs full
capability distinction.
"""
from app.services.email_client_resolver import choose_outbound


SMTP_ONLY_CONFIG = {
    "from_address": "analyst@acme.com",
    "from_name": "Acme Analyst",
    "smtp_host": "smtp.acme.com",
    "smtp_port": 587,
    "inbound_enabled": False,
    "capabilities": ["send"],
}
SMTP_ONLY_CREDS = {
    "smtp_host": "smtp.acme.com",
    "smtp_port": 587,
    "smtp_username": "analyst@acme.com",
    "smtp_password": "secret",
    "from_address": "analyst@acme.com",
}

FULL_CONFIG = {**SMTP_ONLY_CONFIG, "inbound_enabled": True, "imap_host": "imap.acme.com",
               "capabilities": ["send", "receive"]}
FULL_CREDS = {**SMTP_ONLY_CREDS, "imap_host": "imap.acme.com", "imap_username": "analyst@acme.com",
              "imap_password": "secret"}


def test_integration_overrides_global_when_smtp_present():
    r = choose_outbound(SMTP_ONLY_CONFIG, SMTP_ONLY_CREDS, global_client_present=True)
    assert r.uses_integration is True
    assert r.source == "integration"
    assert r.from_address == "analyst@acme.com"
    assert r.from_name == "Acme Analyst"
    assert r.smtp_config.host == "smtp.acme.com"
    assert r.smtp_config.username == "analyst@acme.com"


def test_smtp_only_still_overrides_global():
    # SMTP-only (no IMAP) must still be the org's outbound transport.
    r = choose_outbound(SMTP_ONLY_CONFIG, SMTP_ONLY_CREDS, global_client_present=True)
    assert r.uses_integration is True


def test_full_integration_also_resolves_outbound():
    r = choose_outbound(FULL_CONFIG, FULL_CREDS, global_client_present=False)
    assert r.uses_integration is True
    assert r.smtp_config.host == "smtp.acme.com"


def test_falls_back_to_global_when_no_integration():
    r = choose_outbound(None, None, global_client_present=True)
    assert r.uses_integration is False
    assert r.source == "global"


def test_no_transport_when_neither_present():
    r = choose_outbound(None, None, global_client_present=False)
    assert r.uses_integration is False
    assert r.source == "none"


def test_from_address_defaults_to_smtp_username():
    creds = {**SMTP_ONLY_CREDS}
    creds.pop("from_address")
    cfg = {**SMTP_ONLY_CONFIG}
    cfg.pop("from_address")
    r = choose_outbound(cfg, creds, global_client_present=False)
    assert r.from_address == "analyst@acme.com"


def test_capabilities_reflect_inbound_flag():
    # Capability is derived from config — SMTP-only => send; +IMAP => send+receive.
    assert SMTP_ONLY_CONFIG["capabilities"] == ["send"]
    assert FULL_CONFIG["capabilities"] == ["send", "receive"]
