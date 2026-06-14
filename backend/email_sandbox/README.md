# Email integration — sandbox feedback loop

A self-contained harness that validates the **real** email mechanics against a
**live local SMTP server** (`aiosmtpd`) using the real `EmailAdapter`, the real
SMTP sender (`aiosmtplib`), and the real poller — without the heavy app stack.

It lives *outside* `backend/tests/` on purpose: `backend/tests/conftest.py`
imports the full app (psycopg2, SQLAlchemy, …), which won't load in a minimal
sandbox. This harness needs only `pytest`, `pytest-asyncio`, `aiosmtplib`, and
`aiosmtpd`. The heavy modules the adapter touches (`app.settings.config`,
`app.models`) are stubbed in `conftest.py`; everything in
`app/services/email/*` and the `EmailAdapter` run for real.

## Run it

```bash
pip install pytest pytest-asyncio aiosmtplib aiosmtpd
cd backend/email_sandbox
python -m pytest            # asyncio_mode=auto via pytest.ini
```

Expected: **48 passed**.

## What each file proves

| File | Covers |
|---|---|
| `test_email_security.py` | DMARC/DKIM/SPF verdict parsing; spoof rejection; domain allowlist; auto-reply/loop/list suppression; audit metadata. |
| `test_email_resolver.py` | Org Email integration **overrides global SMTP**; SMTP-only still overrides; fallback to global; capability tiers (`send` vs `send+receive`). |
| `test_email_poller.py` | Authentic mail routed to the agent; spoof/off-allowlist blocked; duplicate `Message-ID` skipped — all with a `FakeMailboxReader`, no IMAP server. |
| `test_email_adapter.py` | Inbound parse; new-thread vs reply (`References` root); quoted-history + signature stripping; sender-as-identity. |
| `test_email_oauth.py` | XOAUTH2 SASL formatting; Microsoft client-credentials token (mocked HTTP); Google service-account dispatch (mocked); `SmtpConfig`/`ImapConfig` carry the OAuth settings; provider dispatch. |
| `test_email_sandbox_loop.py` | **End-to-end against a live SMTP server:** (1) SMTP-only sends + overrides global; (2) full integration: inbound → poller → adapter → threaded reply chained via `In-Reply-To`/`References`; (3) agent-initiated email → user reply re-attaches to the same report via the thread root; (4) spoofed reply blocked at the boundary. |

## How it maps to the requirements

- **Both SMTP-only and full integration** — `test_smtp_only_*` vs the inbound/
  reply tests in `test_email_sandbox_loop.py` + `test_email_poller.py`.
- **User auth + auto-link** — identity is the verified `From` address
  (`get_user_info`); the manager auto-links to an existing member like Teams but
  never auto-provisions from email (see `docs/design/email-integration.md` §
  Security). The auth gate (DMARC/allowlist) is in `test_email_security.py`.
- **Security + metadata** — `test_email_security.py` + the `security` metadata
  asserted in `test_email_poller.py` / `test_email_sandbox_loop.py`.
- **Agent sends first, user reply attaches to the right report** —
  `test_agent_initiated_then_user_reply_reattaches`.

## Extending

The SMTP sink (`smtp_sink` fixture) captures raw bytes of everything sent, so
new outbound assertions just parse `handler.messages[-1]`. Inbound scenarios use
`FakeMailboxReader.deliver(raw_bytes)` then drive `EmailPoller.poll_once()`.

## Full-stack validation (optional)

To exercise the DB-backed manager/route layer too, install the full stack
(`apt-get install -y libpq-dev && pip install --upgrade setuptools && pip
install -r ../requirements_versioned.txt`) and add e2e tests under
`backend/tests/` following `tests/e2e/test_report_notifications.py`. Not
required for the mechanics above, which this harness covers directly.
