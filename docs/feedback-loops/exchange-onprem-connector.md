# Feedback Loop — Exchange Server (on-prem) mail connector

New connection type `exchange_onprem`: read-only mail from an on-premises
Exchange server over Exchange Web Services, with Windows authentication, backing
the existing `list_emails` / `search_email` / `read_email` agent tools.
Design: [`docs/design/exchange-onprem-mail-connector.md`](../design/exchange-onprem-mail-connector.md).
Admin guide: [`docs/exchange-server.md`](../exchange-server.md).

**No Exchange server was available.** Everything below ran against a local
endpoint that speaks the same protocol behind a real Windows (NTLM) sign-in.
What that does and does not prove is stated in the last section.

## The loop

Three layers, each runnable on its own.

### A — deterministic contract tests (no network)

```bash
cd backend
export BOW_DATABASE_URL='sqlite:///db/unit.db' TESTING=true
uv run pytest tests/unit/test_exchange_onprem_client.py \
              tests/unit/test_exchange_onprem_schema_context.py -q        # 121 tests
uv run pytest tests/e2e/test_exchange_onprem.py -m e2e --db=sqlite -q    # 5 tests
uv run pytest tests/unit/test_connector_egress.py -k exchange -q         # egress pinning
```

The HTTP boundary is replaced by `tests/fixtures/fake_exchange.py`, an in-memory
provider that **evaluates** each request against a mailbox model — folder scope,
paging, unread / received restrictions, keyword queries, per-mailbox access —
rather than returning canned replies. A wrong request yields a wrong result.

Covered: folder trees across mailboxes; shared, unreachable and folder-level-shared
mailboxes; localized servers; listing and search paging to completion under
server page caps and one-folder-per-request servers; filters; reading (headers,
link, attachments, HTML bodies, control characters); the lockout guard; server
busy back-off; HTTP and TLS failures; response size and entity-expansion limits;
URL and address validation; the API lifecycle, the member boundary, and
user-required credentials through the real routes.

The SharePoint Server (on-prem) suites pass unchanged after its session code
moved to the shared helper.

Each behaviour was confirmed to be *tested*, not merely *passing*: ten in-memory
mutations (ignore incomplete pages, drop the tie-skip, list sent mail, search
deleted mail, disable the lockout guard, treat calendars as mail, drop filters,
drop the back-off, keep HTML, drop the shared-mailbox prefix) each fail between
1 and 14 tests.

### B — the real client against a real Windows sign-in

```bash
cd backend
uv run python ../tools/agent/mock_exchange_server.py --port 8443 --tls \
    --require-channel-binding --cert-out /tmp/exchange-ca.pem --log /tmp/exchange.jsonl
```

The endpoint serves the same provider over HTTPS with a throwaway certificate
and a genuine NTLM handshake. With `--require-channel-binding` it refuses any
sign-in that is not bound to the TLS connection — what Extended Protection
enforces. Three lab identities see different mailboxes (see the script header).

Pointing `ExchangeOnpremClient` at it, with the certificate in
`REQUESTS_CA_BUNDLE`:

| Check | Result |
|---|---|
| Sign in over TLS with channel binding required | Accepted; server log records `channel_bound: true` |
| **Control:** same sign-in with the binding switched off | **Refused** — so the line above proves the binding is sent and correct |
| `dana` (may open support@, not sales@) | Own mailbox and support@ listed; sales@ reported as not reachable |
| `noa` (no shared mailboxes) | Own mailbox only; support@ reported as not reachable |
| `svc-mail`, own mailbox excluded | support@ is the default mailbox; sales@ listed by address |
| Wrong password, connection test run twice | Both fail with the same message; the server log shows **one** refused sign-in |

### C — the product, end to end, with a real LLM

```bash
# backend: stock app + in-process enterprise license (the connector is enterprise-gated)
cd backend
REQUESTS_CA_BUNDLE=/tmp/ca-bundle.pem BOW_ENCRYPTION_KEY=<fernet key> \
BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium \
  uv run python ../tools/agent/exchange_onprem_sandbox.py
# frontend
cd frontend && yarn dev
# org, users, model
cd backend && uv run python ../tools/agent/seed_org.py --invite member@example.com \
    --db-path db/exchange_onprem_sandbox.db
# then configure an LLM provider and a default model (Settings → Models, or one
# of the tools/agent/setup_*_llm.py scripts with the key in the environment)
```

`/tmp/ca-bundle.pem` is the endpoint's certificate followed by the environment's
own bundle, so the backend can verify both the mail endpoint and the LLM
provider. Driven through the UI with Playwright; every `/api/` response and the
endpoint's request log were recorded.

| Step | Layer checked | Result |
|---|---|---|
| Agents → Add connection → search "exchange" → fill the form → Connect | UI, `POST /api/data_sources/test_connection`, `POST /api/connections` | 200 / 200; endpoint log shows `svc-mail` signing in |
| Same form in Hebrew | UI | Right-to-left, connector strings translated |
| Admin asks the team-inbox agent which support mail is unread and most urgent | UI, `tool_executions`, endpoint log, LLM calls | `list_emails` succeeded; 4 unread found across Inbox and Inbox/Escalations; only `svc-mail` reached the endpoint; the LLM calls used the configured default model |
| Admin asks for a search across mailboxes | same | One search request covered every folder; each hit named with its mailbox address and attachments |
| Member opens the agent selector | UI | The personal-mail agent is unavailable and offers **Connect** |
| Member clicks Connect, enters own Windows credentials, saves | UI, `POST /api/connections/{id}/my-credentials` | 200 |
| Member asks about own unread mail and whether support@ is visible | UI, endpoint log | Own mail returned; agent says support@ is **not** reachable for this account; **only `noa`** reached the endpoint |
| Member's stored password made wrong; member asks for three things | UI, `tool_executions`, endpoint log | Three tool calls failed with the "do not retry" message; the agent relayed it; the endpoint saw **one** refused sign-in |
| Database after all runs | DB | 0 catalog rows for the mail connections; credentials stored encrypted; lab passwords absent in plaintext |

## What the live run changed

Four defects were found by running the agent, none by the unit tests as first
written. Each now has a test.

1. **Unread mail in a subfolder was missed.** A listing with no folder covered
   only the Inbox; an escalated message filed under `Inbox/Escalations` was not
   reported. A listing with no folder now covers every incoming folder of the
   mailbox, paged by received time.
2. **A team mailbox was called "your Inbox".** The planner could not tell a
   service-account connection from a personal one. Its context now states which
   it is and names the mailboxes.
3. **An unreachable shared mailbox was reported as visible.** It was skipped
   silently. Folder listings, whole-mailbox listings and connection-wide
   searches now name what could not be opened.
4. **A server that reports a multi-folder refusal per folder** (rather than for
   the whole request) was not recognized, so search failed instead of falling
   back to one folder per request.

## UI evidence

New screens only; no existing screen changed appearance. Images are under
`media/pr/exchange-onprem-mail/`.

| | |
|---|---|
| Catalog tile | `01-catalog-tile.png` |
| Connection form (English / Hebrew) | `02-connection-form-en.png`, `03-connection-form-he-rtl.png` |
| Team inbox answer | `04-agent-team-inbox.png` |
| Member must connect; Connect dialog | `05-member-must-connect.png`, `06-member-connect-dialog.png` |
| Personal mail answer, shared mailbox not reachable | `07-agent-personal-mail.png` |
| Wrong password: no retry | `08-wrong-password-no-retry.png` |
| Search across mailboxes | `09-agent-search.png` |

## What still needs a real Exchange Server

The local endpoint implements the documented request and response shapes, but it
was written from the same reading of the protocol as the client. These points
can only be settled against a real server, and the connector stays in beta until
they are:

1. **Sign-in against Exchange with Extended Protection on**, directly and behind
   the customer's load balancer. The binding itself is proven correct above; the
   server-side policy and proxy path are not.
2. **Keyword search across several folders in one request.** If the server
   refuses it the client falls back to one folder per request automatically
   (tested), at the cost of more round-trips.
3. **Keyword query syntax** as the server's search index interprets it
   (`from:`, `subject:`, `OR`, phrases) and how fresh the index is.
4. **The listing extras** (body preview and web link in listings). If the server
   refuses them the client drops them once and carries on (tested).
5. **One request schema version across 2013, 2016, 2019 and Subscription
   Edition.**
6. **Kerberos** sign-in to the mail service with the deployment's keytab. The
   session code is the one SharePoint Server (on-prem) already uses.
7. **Throttling** behaviour under a realistic mailbox size.

Suggested first run on a real server: the connection test, then a listing with
no folder, a search with no folder, and reading one message — as a service
account and as one ordinary user.
