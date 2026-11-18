# Feedback Loop — "Can a BigQuery connection use Workload Identity Federation instead of a JSON key?"

A self-hosted operator asked to stop pasting service-account keys into BOW:
keys accumulate, cannot be rotated in one place, and are the only secret the
BigQuery connection needs. The claim being validated: a BigQuery connection can
authenticate as **the deployment's own Google identity** (Application Default
Credentials — an attached service account or a Workload Identity Federation
config), optionally impersonating a target service account, with **no key
stored**, and only where the operator enabled it.

Operator guide: `docs/guides/bigquery-keyless-auth.md`.

## How the loop runs without a Google account

`tools/agent/fake_google_cloud.py` answers as `sts.googleapis.com`,
`iamcredentials.googleapis.com`, `bigquery.googleapis.com` and
`oauth2.googleapis.com` over TLS on `127.0.0.1:443`. The backend, google-auth
and google-cloud-bigquery are **unmodified**: the hostnames are pointed at the
fake through `/etc/hosts`, and the backend is started trusting the fake's
throwaway CA and bypassing the egress proxy for those hosts. So the real
libraries run the real protocol:

```
subject token file ──STS──▶ federated token ──IAM generateAccessToken──▶ service-account token ──▶ BigQuery REST
```

The fake enforces what Google would (wrong subject token → `invalid_grant`,
no Token Creator grant → 403, no BigQuery role → 403) and appends every call to
`<run-dir>/bigquery_audit.jsonl` **with the principal it ran as**. That file is
the proof of which identity a connection used. SQL runs on an in-memory DuckDB
(`sales.customers`, `sales.orders`).

What this does **not** prove: behaviour against real Google IAM, a real GCE /
GKE metadata server, or AWS / Azure-sourced federation. Those paths go through
the same `google.auth.default()` call but were not exercised here.

## Reproduce

```bash
RUN=/tmp/fake-google
cd backend
uv sync --frozen --extra dev

# 1. Fake Google (root: port 443 + /etc/hosts). Prints the env for step 2.
uv run python ../tools/agent/fake_google_cloud.py --run-dir $RUN --write-hosts &

# 2. Stack, with keyless auth enabled and ADC pointed at the federation config
export BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true
export GOOGLE_APPLICATION_CREDENTIALS=$RUN/federation.json
export REQUESTS_CA_BUNDLE=$RUN/ca-bundle.pem SSL_CERT_FILE=$RUN/ca-bundle.pem
export NO_PROXY="$NO_PROXY,sts.googleapis.com,iamcredentials.googleapis.com,bigquery.googleapis.com,oauth2.googleapis.com"
export no_proxy="$NO_PROXY"
export BOW_ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
(cd .. && tools/agent/boot_stack.sh --dev)
uv run python ../tools/agent/seed_org.py

# 3. A scripted LLM that makes the agent run one real BigQuery query
#    (sources.json: [{"id": "<agent id>", "name": "Sales warehouse", "table": "sales.orders"}])
STUB_SOURCES_FILE=/abs/sources.json uv run python ../tools/agent/stub_llm_bigquery.py &
uv run python ../tools/agent/setup_stub_llm.py

# afterwards
uv run python ../tools/agent/fake_google_cloud.py --remove-hosts
```

In a DevBox (`BOW_SANDBOX_DEVBOX=1`) skip `boot_stack.sh` / `seed_org.py`: export
the same variables into the stack's environment and `bow-stack restart backend`.

UI path: **Agents → New → Create new connection → Google BigQuery**, method
**Application Default Credentials**, project `acme-analytics`, dataset `sales`,
service account to impersonate `bq-reader@acme-analytics.iam.gserviceaccount.com`.
Editing an existing connection: **Agents → CONNECTIONS strip → the connection →
Manage connection → Edit settings**.

## What was checked, at each layer

| # | Step (through the UI) | App / HTTP | App database (decrypted credentials) | Google side (`bigquery_audit.jsonl`) |
|---|---|---|---|---|
| 1 | Create keyless connection, target `admin@…` (not impersonable) | Test fails: "Could not impersonate admin@… Grant … Service Account Token Creator …" | nothing saved | `iam.generateAccessToken` REFUSED |
| 2 | Same form, target `bq-reader@…` → Connect | `POST /api/connections` 200, 2 tables discovered | `{use_application_default_credentials: true, target_service_account: bq-reader@…}` — no `credentials_json` | STS ok → IAM ok → `datasets.list` / `jobs.insert` as **SA bq-reader** |
| 3 | Agent wizard → table preview → Run | 8 rows of `sales.orders`, read from table storage | — | not inspected for this step |
| 4 | Chat prompt (scripted LLM → `create_data` → generated code) | "Created Data" step completes | — | the aggregate `jobs.insert` as **SA bq-reader** |
| 5 | A key-based connection (Service Account JSON) → Manage → switch to keyless → Test → Save | `PUT /api/connections/{id}` 200 | `credentials_json` **gone**; marker + target stored | before: `oauth2.token` + calls as SA legacy-key; after: calls as SA bq-reader, no further `oauth2.token` |
| 6 | Switch the same connection back to a key | 200 | marker and target **gone**; key stored | calls as SA legacy-key again |
| 7 | Reopen a keyless connection | form shows the saved target | — | — |
| 8 | Clear the target → Save | `PUT` sends `target_service_account: null` | `{use_application_default_credentials: true}` | calls as the **federated principal** itself |
| 9 | Restart **without** `BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS` | new-connection form offers no method selector (identical to before this change); existing keyless connection's Test: "Application Default Credentials are turned off on this deployment…" | unchanged | **no call reaches Google** |
| 10 | Still disabled: paste a key into that connection → Save | 200 | key stored, marker gone | — |

Failure messages checked directly against the client (`BigqueryClient.test_connection`):
wrong subject token → "The deployment's Google identity could not obtain an
access token… (The subject token is not valid for this provider)"; missing
credential file → "No Application Default Credentials were found on this
deployment…".

## Findings fixed during the loop

- The saved impersonation target was not shown when the dialog was reopened and
  could not be removed (a blank field means "unchanged"). The target is now part
  of the non-secret metadata the edit form receives, and the form sends an
  explicit `null` for a visible field the admin emptied (step 7–8).
- With the opt-in off, the edit form of a keyless connection showed the key
  fields but would still have saved `auth_type: adc`. The form now falls back to
  the default method when the saved one is no longer offered (step 10).
- The method selector was 12rem wide and truncated its own labels; it is 18rem.

## Automated coverage

```bash
cd backend
TESTING=true ENVIRONMENT=production uv run pytest tests/unit/test_bigquery_default_credentials.py -q
TESTING=true ENVIRONMENT=production uv run pytest tests/e2e/rbac/test_bigquery_keyless_auth.py -m e2e --db=sqlite -q
```

Only Google is faked there (`google.auth.default` and `bigquery.Client`).
