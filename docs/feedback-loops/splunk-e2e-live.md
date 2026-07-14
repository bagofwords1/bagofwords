# Feedback Loop — Splunk connector, live end-to-end through the real UI

Claim being validated: the Splunk integration works end-to-end **as a user
experiences it** — connect a real Splunk (docker) through the onboarding UI,
index its schema, then run real LLM completions (Claude Haiku 4.5, live
Anthropic API) that analyze ~133k seeded log events via `create_data` /
`inspect_data`. This is a Loop B (live) validation; the deterministic Loop A
for the client itself is `backend/tests/unit/test_splunk_client.py`.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend && uv run pytest tests/unit/test_splunk_client.py -q
```

Covers SPL normalization, the JSON query envelope, catalog/tstats discovery,
thin-table fallback, and error surfacing — all against stubbed HTTP.

## Loop B — live confirmation (docker Splunk + real LLM)

### 1. Splunk container + LOTS of data

```bash
docker run -d --name splunk -p 8089:8089 -p 8088:8088 \
  -e SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com \
  -e SPLUNK_START_ARGS=--accept-license \
  -e SPLUNK_PASSWORD='<local password>' \
  -e SPLUNK_HEC_TOKEN='<local hec token>' \
  splunk/splunk:latest
# wait for docker inspect -f '{{.State.Health.Status}}' splunk == healthy (~3 min)
cd backend && uv run python ../tools/agent/splunk-e2e/seed_splunk.py   # x3 seeds for volume
```

The seeder creates indexes `web`, `app`, `security`, widens the HEC token to
them, and pushes ~44k events per run spread over 72h (run it 2–3× with the
seed constant changed for ~133k total):

- `web::access_combined` — nginx combined logs, 6 hosts, diurnal traffic
- `app::app_logs` — JSON service logs (level/service/message/latency_ms)
- `security::auth` — webauth logins + brute-force bursts from 3 attacker IPs

A deliberate incident is baked in (payments/checkout `ERROR` spike + web 5xx,
2 days ago, 2h window) so analysis prompts have a real signal.

Observed: `| tstats count where index=* by index, sourcetype` → 58,946 +
37,724 + 36,883 events.

### 2. Stack + org + live LLM

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
# Anthropic provider + default model, key from env only:
# POST /api/llm/providers with models inline (see note below), then
# POST /api/llm/test_connection → {"success": true}
```

### 3. Connect Splunk through the real onboarding UI

```bash
cd frontend && node ../tools/agent/splunk-e2e/ui_connect_splunk.mjs
node ../tools/agent/splunk-e2e/ui_tables_activate.mjs   # Select all → Save (3/3 active)
```

Observed (screenshots in `media/pr/ai-ecstatic-sagan-84i4pc/`):

- `06-test-connection-ok.png` — Test Connection → **"Connected successfully.
  Found 3 tables."** (userpass auth, verify_ssl off for the self-signed cert)
- `08-schema-tables.png` — schema discovery lists `security::auth`,
  `app::app_logs`, `web::access_combined`
- `13-tables-activated.png` — 3/3 active after Select all → Save

### 4. Real completions over the logs (UI, live LLM)

```bash
node ../tools/agent/splunk-e2e/ui_run_prompt.mjs "<prompt>" <shot-prefix>
```

| Prompt | Result (all `status=success`) |
|---|---|
| Top pages by traffic + 5xx by host | 2 `create_data` runs; chart of 9 URIs (`/api/products` #1, matching the seeded 25% weight); 5xx split across web-01…web-06 (`q1-*.png`) |
| Investigate the payments incident "two days ago" | `inspect_data` on `app::app_logs`, then **self-corrected field names** (`log_level` → `level`) and re-queried; 6 saved queries incl. hourly error timeline; correctly pinned the spike to July 12 with payments/checkout/cart most affected (`q2-*.png`) |
| Brute-force login attacks? | `create_data` with SPL `search index=security sourcetype=auth action=failure | stats count as failed_attempt_count by src_ip, user | sort -…` — surfaced **exactly the 3 seeded attacker IPs** (45.155.205.99, 185.220.101.34, 91.240.118.172) targeting admin/root/test (`q3-*.png`, SPL visible in `q3-7-spl-code.png`) |

The q2 self-correction is the schema-on-read loop from
`splunk_client.system_prompt()` working as designed: empty result → field
discovery → correct query, without asking the user.

## What this proves / regression notes

Proves: connector catalog → ConnectForm (userpass variant, verify_ssl toggle)
→ `/api/data_sources/test_connection` → schema indexing (tstats catalog +
fieldsummary sampling) → table activation → agentic completions issuing real
SPL through `create_data`/`inspect_data` → charts, saved queries, and learned
instructions. All on Splunk 10.4.1.

Pre-existing issues hit along the way (not Splunk-related, reproduce on main):

1. `POST /api/llm/models` → 500: `app/routes/llm.py:130` calls
   `llm_service.create_model`, which does not exist on `LLMService`
   (only `_create_models`). The UI never hits this (it sends models inline on
   provider create), but the route is dead on arrival.
2. Creating an LLM provider with the same name as a **soft-deleted** provider
   → 500 (`UNIQUE constraint failed: llm_providers.organization_id, name`,
   surfaced via a `MissingGreenlet` during error handling). Deleting a
   provider then re-adding it under the same name is a plausible user flow;
   it should either 409 cleanly or exclude soft-deleted rows from the
   constraint.
