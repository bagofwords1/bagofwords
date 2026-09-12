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

Expected: **65 passed**.

## What each file proves

| File | Covers |
|---|---|
| `test_email_security.py` | DMARC/DKIM/SPF verdict parsing; spoof rejection; domain allowlist; auto-reply/loop/list suppression; audit metadata. |
| `test_email_resolver.py` | Org Email integration **overrides global SMTP**; SMTP-only still overrides; fallback to global; capability tiers (`send` vs `send+receive`). |
| `test_email_poller.py` | Authentic mail routed to the agent; spoof/off-allowlist blocked; duplicate `Message-ID` skipped — all with a `FakeMailboxReader`, no IMAP server. |
| `test_email_adapter.py` | Inbound parse; new-thread vs reply (`References` root); quoted-history + signature stripping; sender-as-identity. |
| `test_email_oauth.py` | XOAUTH2 SASL formatting; Microsoft client-credentials token (mocked HTTP); Google service-account dispatch (mocked); `SmtpConfig`/`ImapConfig` carry the OAuth settings; provider dispatch. |
| `test_email_sandbox_loop.py` | **End-to-end against a live SMTP server:** (1) SMTP-only sends + overrides global; (2) full integration: inbound → poller → adapter → threaded reply chained via `In-Reply-To`/`References`; (3) agent-initiated email → user reply re-attaches to the same report via the thread root; (4) spoofed reply blocked at the boundary. |
| `test_smtp_probe_honesty.py` | **Every configuration that the old connect-and-auth probe called "Connection OK" while being unable to deliver:** wrong password, missing password (a rotated Fernet key), an unauthorised envelope sender, relay-denied recipient, a STARTTLS-required relay, a missing From address, an unreachable host. Each must now come back as a failure, tagged with the stage that explains it. |
| `relay.py` | The rejecting relay these tests run against (not a test itself). |

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

### A relay that can say no

`smtp_sink` accepts everything, which is fine for "did we build the right MIME"
and useless for "does this configuration actually deliver". `relay.run_relay()`
is the sink that refuses:

```python
from relay import run_relay

with run_relay(require_auth=("user", "pw"),
               allowed_senders=["noreply@acme.com"],
               allowed_rcpts=["admin@acme.com"],
               require_tls=True) as relay:
    ...                      # relay.host, relay.port, relay.count, relay.handler
```

Each knob models a refusal real servers hand out — `535` on bad credentials,
`550` on an unowned sender or a recipient it will not relay to, `530` while the
session is in plaintext. A relay that cannot reject cannot distinguish a fixed
test from a broken one, which is exactly how the old settings probe shipped.

### Two sinks, not one

To prove *which* transport carried a message, run two relays on different ports:
one standing in for the organization's own SMTP server and one for the global
bow-config client. "The invite went to the org relay" is then a mechanical
assertion (`org.count == 1 and global.count == 0`) rather than a guess — with a
single sink the two are indistinguishable, which is how org SMTP came to be
silently bypassed for invites and shares.

> **Do not set `BOW_EMAIL_SMTP_OVERRIDE_HOST` in an end-to-end run.** It rewrites
> whatever host is configured to a local sink, so the run proves nothing about
> the host the admin actually typed — a nonsense hostname "succeeds". See
> `test_sandbox_override_is_not_silently_applied`, which asserts that effect
> explicitly so nobody relies on it by accident.

## Full-stack validation (optional)

To exercise the DB-backed manager/route layer too, install the full stack
(`apt-get install -y libpq-dev && pip install uv && uv sync --frozen --extra dev`) and add e2e tests under
`backend/tests/` following `tests/e2e/test_report_notifications.py`. Not
required for the mechanics above, which this harness covers directly.
