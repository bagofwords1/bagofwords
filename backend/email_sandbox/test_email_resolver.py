"""Purpose-aware outbound resolution: analyst vs system, org SMTP override.

Covers the requirements: backward orgs (no DB SMTP) fall back to global; orgs
that set DB SMTP use it even when the global bow-config SMTP is empty; analyst
mail always uses the AI mailbox.
"""
from app.services.email_client_resolver import choose_outbound


AI_CONFIG = {"from_address": "analyst@acme.com", "from_name": "Acme Analyst", "smtp_host": "smtp.acme.com"}
AI_CREDS = {"smtp_host": "smtp.acme.com", "smtp_port": 587, "smtp_username": "analyst@acme.com",
            "smtp_password": "x", "from_address": "analyst@acme.com"}

ORG_SMTP = {"enabled": True, "host": "relay.acme.com", "port": 587, "security": "starttls",
            "username": "noreply@acme.com", "password": "secret",
            "from_address": "noreply@acme.com", "from_name": "Acme"}


# ---- analyst mail ----

def test_analyst_uses_ai_mailbox():
    r = choose_outbound("analyst", AI_CONFIG, AI_CREDS, ORG_SMTP, global_present=True)
    assert r.source == "ai_mailbox"
    assert r.from_address == "analyst@acme.com"
    assert r.smtp_config.host == "smtp.acme.com"
    assert r.uses_smtp_config


def test_analyst_without_ai_mailbox_falls_through():
    # No AI mailbox -> analyst mail still goes out via system precedence.
    r = choose_outbound("analyst", None, None, ORG_SMTP, global_present=True)
    assert r.source == "org_smtp"


# ---- system mail ----

def test_system_never_uses_ai_mailbox():
    # Even though an AI mailbox exists, system mail must NOT use it.
    r = choose_outbound("system", AI_CONFIG, AI_CREDS, ORG_SMTP, global_present=True)
    assert r.source == "org_smtp"
    assert r.from_address == "noreply@acme.com"


def test_system_org_smtp_overrides_global():
    r = choose_outbound("system", None, None, ORG_SMTP, global_present=True)
    assert r.source == "org_smtp"
    assert r.smtp_config.host == "relay.acme.com"
    assert r.smtp_config.password == "secret"


def test_system_org_smtp_works_when_global_empty():
    # Org set DB SMTP, bow-config global is empty -> still use org SMTP.
    r = choose_outbound("system", None, None, ORG_SMTP, global_present=False)
    assert r.source == "org_smtp"


def test_backward_no_org_smtp_falls_back_to_global():
    r = choose_outbound("system", None, None, None, global_present=True)
    assert r.source == "global"
    assert not r.uses_smtp_config
    assert r.uses_global


def test_system_disabled_org_smtp_falls_back_to_global():
    disabled = {**ORG_SMTP, "enabled": False}
    r = choose_outbound("system", None, None, disabled, global_present=True)
    assert r.source == "global"


def test_nothing_configured_is_none():
    r = choose_outbound("system", None, None, None, global_present=False)
    assert r.source == "none"
    assert not r.uses_smtp_config
