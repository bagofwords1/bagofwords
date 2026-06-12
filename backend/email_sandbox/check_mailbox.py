"""Standalone mailbox connectivity check — run this with YOUR credentials.

Run it on a machine that can reach your mail provider (e.g. inside your network),
in the backend venv (needs httpx, aiosmtplib, google-auth). It mints a token,
authenticates SMTP+IMAP via XOAUTH2 (or password), sends a test email, and lists
unread mail — printing each step. **Paste the OUTPUT back; it contains no secrets.**

Microsoft 365 (app-only):
    EMAIL_AUTH=microsoft \
    EMAIL_MAILBOX=analyst@acme.com \
    EMAIL_TEST_TO=you@acme.com \
    EMAIL_MS_TENANT=<tenant-id> EMAIL_MS_CLIENT_ID=<client-id> EMAIL_MS_CLIENT_SECRET=<secret> \
    python check_mailbox.py

Google Workspace (service account + DWD):
    EMAIL_AUTH=google \
    EMAIL_MAILBOX=analyst@acme.com \
    EMAIL_TEST_TO=you@acme.com \
    EMAIL_GOOGLE_SA_JSON=/path/to/service-account.json \
    python check_mailbox.py

Password (on-prem / app password):
    EMAIL_AUTH=password EMAIL_MAILBOX=analyst@acme.com EMAIL_TEST_TO=you@acme.com \
    EMAIL_SMTP_HOST=... EMAIL_SMTP_PORT=587 EMAIL_SMTP_USER=... EMAIL_SMTP_PASS=... \
    EMAIL_IMAP_HOST=... EMAIL_IMAP_PORT=993 \
    python check_mailbox.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.email.sender import SmtpConfig, send_message
from app.services.email.mailbox_reader import ImapConfig, ImapMailboxReader
from app.services.email.message_builder import build_email
from app.services.email.oauth import get_xoauth2_string


def _build_creds_and_config():
    auth = os.environ.get("EMAIL_AUTH", "password")
    mailbox = os.environ["EMAIL_MAILBOX"]
    creds = {
        "auth_type": auth,
        "from_address": mailbox,
        "smtp_username": os.environ.get("EMAIL_SMTP_USER", mailbox),
        "imap_username": os.environ.get("EMAIL_IMAP_USER", mailbox),
        "smtp_password": os.environ.get("EMAIL_SMTP_PASS"),
        "imap_password": os.environ.get("EMAIL_IMAP_PASS") or os.environ.get("EMAIL_SMTP_PASS"),
        "smtp_host": os.environ.get("EMAIL_SMTP_HOST"),
        "smtp_port": os.environ.get("EMAIL_SMTP_PORT"),
        "imap_host": os.environ.get("EMAIL_IMAP_HOST"),
        "imap_port": os.environ.get("EMAIL_IMAP_PORT"),
        "smtp_security": os.environ.get("EMAIL_SMTP_SECURITY", "starttls"),
    }
    config = {}
    if auth == "microsoft":
        creds.update(
            ms_tenant_id=os.environ["EMAIL_MS_TENANT"],
            ms_client_id=os.environ["EMAIL_MS_CLIENT_ID"],
            ms_client_secret=os.environ["EMAIL_MS_CLIENT_SECRET"],
        )
        config.update(smtp_host="smtp.office365.com", smtp_port=587, smtp_security="starttls",
                      imap_host="outlook.office365.com", imap_port=993, imap_use_ssl=True)
    elif auth == "google":
        with open(os.environ["EMAIL_GOOGLE_SA_JSON"]) as f:
            creds["google_service_account_info"] = json.load(f)
        config.update(smtp_host="smtp.gmail.com", smtp_port=587, smtp_security="starttls",
                      imap_host="imap.gmail.com", imap_port=993, imap_use_ssl=True)
    return auth, mailbox, creds, config


async def main():
    auth, mailbox, creds, config = _build_creds_and_config()
    to_addr = os.environ.get("EMAIL_TEST_TO", mailbox)
    smtp = SmtpConfig.from_credentials(creds, config)
    imap = ImapConfig.from_credentials(creds, config)

    print(f"[1] auth={auth} mailbox={mailbox}")
    print(f"    SMTP {smtp.host}:{smtp.port} ({smtp.security}) | IMAP {imap.host}:{imap.port}")

    if auth in ("microsoft", "google"):
        try:
            sasl = await get_xoauth2_string(smtp.oauth)
            print(f"[2] OAuth token minted OK (XOAUTH2 string {len(sasl)} chars)")
        except Exception as e:
            print(f"[2] FAILED to mint OAuth token: {e}")
            return

    print(f"[3] Sending test email to {to_addr} ...")
    msg = build_email(
        from_address=mailbox, from_name="BOW Mailbox Check", to_address=to_addr,
        subject="BOW mailbox connectivity test",
        body="If you received this, outbound SMTP works.",
    )
    ok = await send_message(smtp, msg)
    print(f"    SMTP send: {'OK' if ok else 'FAILED'}")

    if imap.host:
        print("[4] IMAP: connecting + listing unread ...")
        try:
            msgs = await ImapMailboxReader(imap).fetch_unseen()
            print(f"    IMAP OK — {len(msgs)} unread message(s) in INBOX")
        except Exception as e:
            print(f"    IMAP FAILED: {e}")
    else:
        print("[4] IMAP host not set — skipping inbound check")

    print("[done]")


if __name__ == "__main__":
    asyncio.run(main())
