"""EmailAdapter: inbound parsing, threading, identity, body cleaning."""
import pytest

CREDS = {
    "smtp_host": "127.0.0.1",
    "smtp_port": 2525,
    "smtp_username": "analyst@bow.test",
    "smtp_password": "x",
    "smtp_security": "none",
    "from_address": "analyst@bow.test",
}
CONFIG = {
    "from_address": "analyst@bow.test",
    "from_name": "BOW Analyst",
    "allowed_domains": ["acme.com"],
}


def _raw(*, frm="Alice <alice@acme.com>", subject="Revenue?", msg_id="<u1@acme.com>",
         references=None, in_reply_to=None, body="What was Q3 revenue?"):
    headers = [
        f"From: {frm}",
        "To: analyst@bow.test",
        f"Subject: {subject}",
        f"Message-ID: {msg_id}",
        "Authentication-Results: mx; dmarc=pass; dkim=pass",
    ]
    if references:
        headers.append(f"References: {references}")
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    return ("\n".join(headers) + "\n\n" + body).encode()


async def test_parse_top_level_message_starts_new_thread(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    out = await a.process_incoming_message({"raw": _raw()})
    assert out["platform_type"] == "email"
    assert out["external_user_id"] == "alice@acme.com"
    assert out["external_email"] == "alice@acme.com"
    assert out["channel_id"] == "alice@acme.com"
    assert out["channel_type"] == "im"
    assert out["message_text"] == "What was Q3 revenue?"
    # New thread: root is the message's own id.
    assert out["thread_ts"] == "<u1@acme.com>"
    assert out["is_thread_reply"] is False
    assert out["auth_results"]["dmarc"] == "pass"
    assert out["from_domain"] == "acme.com"


async def test_parse_reply_uses_references_root(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    out = await a.process_incoming_message(
        {"raw": _raw(msg_id="<u2@acme.com>", references="<root@bow.test> <u1@acme.com>",
                     in_reply_to="<u1@acme.com>")}
    )
    assert out["is_thread_reply"] is True
    # Conversation root is the first id in References.
    assert out["thread_ts"] == "<root@bow.test>"
    assert out["message_ts"] == "<u2@acme.com>"


async def test_quoted_history_and_signature_stripped(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    body = (
        "Here is my actual question about margins.\n"
        "\n"
        "On Mon, Jun 2, 2026 at 9:00 AM, Analyst wrote:\n"
        "> previous answer text\n"
        "> more quoted text\n"
        "\n"
        "-- \n"
        "Alice | VP Finance | Acme\n"
    )
    out = await a.process_incoming_message({"raw": _raw(body=body)})
    assert out["message_text"] == "Here is my actual question about margins."


async def test_get_user_info_returns_sender_as_email(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    info = await a.get_user_info("alice@acme.com")
    assert info["email"] == "alice@acme.com"


async def test_allowed_domains_exposed(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    assert a.allowed_domains() == ["acme.com"]


async def test_missing_sender_returns_none(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    raw = b"To: analyst@bow.test\nSubject: x\nMessage-ID: <n@x>\n\nbody"
    out = await a.process_incoming_message({"raw": raw})
    assert out is None


# ---------- inbound attachments ----------

from email.message import EmailMessage


def _raw_multipart(filename="data.csv", payload=b"a,b\n1,2\n", maintype="text",
                   subtype="csv", body="check this"):
    m = EmailMessage()
    m["From"] = "Alice <alice@acme.com>"
    m["To"] = "analyst@bow.test"
    m["Subject"] = "Fwd: data"
    m["Message-ID"] = "<att1@acme.com>"
    m["Authentication-Results"] = "mx; dmarc=pass; dkim=pass"
    m.set_content(body)
    m.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return m.as_bytes()


async def test_attachment_extracted(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    out = await a.process_incoming_message({"raw": _raw_multipart()})
    assert out["message_text"] == "check this"
    atts = out["attachments"]
    assert len(atts) == 1
    assert atts[0]["filename"] == "data.csv"
    assert atts[0]["content"] == b"a,b\n1,2\n"
    assert atts[0]["content_type"] == "text/csv"
    assert out["attachments_skipped"] == []


async def test_attachment_oversized_skipped(make_email_adapter, monkeypatch):
    import app.services.platform_adapters.email_adapter as ea
    monkeypatch.setattr(ea, "_MAX_ATTACHMENT_BYTES", 3)
    a = make_email_adapter(CREDS, CONFIG)
    out = await a.process_incoming_message({"raw": _raw_multipart(payload=b"way too big")})
    assert out["attachments"] == []
    assert len(out["attachments_skipped"]) == 1
    assert out["attachments_skipped"][0]["filename"] == "data.csv"


async def test_attachment_count_capped(make_email_adapter, monkeypatch):
    import app.services.platform_adapters.email_adapter as ea
    monkeypatch.setattr(ea, "_MAX_ATTACHMENTS", 1)
    m = EmailMessage()
    m["From"] = "Alice <alice@acme.com>"
    m["To"] = "analyst@bow.test"
    m["Subject"] = "two files"
    m["Message-ID"] = "<m2@acme.com>"
    m["Authentication-Results"] = "mx; dmarc=pass"
    m.set_content("two files")
    m.add_attachment(b"one", maintype="text", subtype="plain", filename="a.txt")
    m.add_attachment(b"two", maintype="text", subtype="plain", filename="b.txt")
    a = make_email_adapter(CREDS, CONFIG)
    out = await a.process_incoming_message({"raw": m.as_bytes()})
    assert len(out["attachments"]) == 1
    assert len(out["attachments_skipped"]) == 1


async def test_plain_email_has_no_attachments(make_email_adapter):
    a = make_email_adapter(CREDS, CONFIG)
    out = await a.process_incoming_message({"raw": _raw()})
    assert out["attachments"] == []
    assert out["attachments_skipped"] == []
