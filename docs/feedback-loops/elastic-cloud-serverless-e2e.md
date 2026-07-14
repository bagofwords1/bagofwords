# Feedback Loop — Elastic Cloud Serverless end-to-end ("connect Elastic, run completions, do RCA on the logs")

Live verification of the Elasticsearch connector against a real **Elastic Cloud
Serverless** observability project (ES 9.5.0), driven entirely through the
product UI with Playwright: connect the data source → run agent completions on
a real Anthropic model → perform a log root-cause analysis and confirm the
agent tools (`create_data`, `inspect_data`, notes/docs/instructions) execute
correctly. The happy path **passed end-to-end**; five product issues were found
along the way (§Findings), two of them with self-contained reproductions below.

## What the live run proved (Loop B, real credentials)

Environment: `tools/agent/boot_stack.sh` + `seed_org.py`, Anthropic provider
with `claude-haiku-4-5` as default model, Elastic Cloud Serverless project
seeded with `tools/elastic/seed_elastic_cloud.py` (below).

- **Connect (UI)**: AddConnectionModal → Elasticsearch → full URL as Host +
  encoded API key → *Test Connection* → "Connected successfully" → schema
  discovery "Discovered 8 tables in 8s" (all `logs-*-default` data streams,
  columns flattened from serverless logsdb mappings) → table activation →
  agent created. Data streams surface correctly as union tables
  (`_discover_data_streams`, `elasticsearch_client.py:244`).
- **Guardrails**: with a write-only key (the serverless *onboarding* key), the
  same flow correctly reports "Connected successfully. Found 0 collections."
  and refuses to create the connection ("Connected but no tables found") —
  connectivity and schema access are distinguished properly at creation time.
- **RCA completion**: the prompt "we saw elevated errors this morning — root
  cause?" produced plan → `create_data` (multi-index DSL aggregation across
  all six streams, 64 rows + chart) → `create_data` (minute-level drill-down)
  → doc artifact. The agent identified the seeded incident exactly: backend
  `db connection pool exhausted` starting **09:12:08 UTC on host-03**,
  cascading to frontend `upstream timeout` 502s ~2 minutes later, and on a
  follow-up quoted the seeded deploy marker verbatim
  (`config change: db pool size 50 -> 20`, 09:08:41Z) with exact error counts
  (420/421) — all matching ground truth queried directly from Elasticsearch.
- **Tool matrix**: `create_note`, `edit_note`, `create_data` (×4),
  `inspect_data` (raw-document sampling, no widget), `create_doc`,
  `search_instructions` (×4), `create_instruction` — all `success`, zero
  connector errors.
- **Error recovery**: one agent-authored query aggregated on `error.message`
  and got ES 400 `match_only_text fields do not support sorting and
  aggregations`; the agent read the error, switched strategy, and completed
  (see finding 3).

## Seeding an empty Elastic Cloud Serverless project

```bash
export ES_URL="https://<project>.es.<region>.gcp.elastic.cloud:443"
export ES_API_KEY_ENCODED="<encoded api key>"        # never commit/echo
# optional: export ES_CA_BUNDLE=/path/to/proxy-ca.crt
python3 tools/elastic/seed_elastic_cloud.py
```

~8.5k ECS-style docs into `logs-{frontend,backend,payments,checkout,auth,search}-default`
data streams: 5 days of baseline traffic ending "today", plus a deliberate
incident for RCA (backend db-pool exhaustion on host-03 09:12–10:45 UTC with a
preceding deploy marker, cascading frontend 502s). Serverless quirks handled:
no `_cluster/health`, no shard settings, data streams only (`op_type=create`).

Key-privilege gotcha: the serverless **onboarding** API key is write-only into
`logs-*-*` (`auto_configure` + `create_doc`) — good enough to seed, but the
connector needs a key with `read` + `view_index_metadata` (an unrestricted
personal key works). `GET /` succeeds with either, so "Test connection" passing
does not imply schema access.

## Findings

### 1. `POST /api/llm/models` is broken — 500 on any custom-model create
`app/routes/llm.py:130` calls `llm_service.create_model(...)`, but
`LLMService` has no such method (only `_create_models`, the provider-payload
path): `AttributeError: 'LLMService' object has no attribute 'create_model'.
Did you mean: '_create_models'?`. The UI works because it sends models inside
the provider payload; anything using the documented model endpoint 500s.

Repro (self-contained, no Elastic needed) — boot the stack, seed an org, then:

```bash
cd backend && uv run python - <<'EOF'
import httpx
c = httpx.Client(base_url="http://localhost:8000", timeout=30)
tok = c.post("/api/auth/jwt/login", data={"username":"admin@example.com","password":"Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
r = c.post("/api/llm/models", json={"provider_id": "<any provider id>", "name": "x",
                                    "model_id": "claude-haiku-4-5-20251001"}, headers=H)
print(r.status_code)   # 500, AttributeError in the body
EOF
```

### 2. Default `BOW_ENCRYPTION_KEY` is per-process — a restart bricks every stored credential
When `BOW_ENCRYPTION_KEY` is unset, config resolution generates a fresh Fernet
key and stashes it in `os.environ` only (`app/settings/config.py:73-77`), so
each backend process gets a different key. After any restart, credentials
encrypted by the previous process fail to decrypt (`InvalidToken`): LLM calls
die (`Failed to decrypt credentials for provider 'anthropic'`,
`app/ai/llm/llm.py:77`), `POST /completions/estimate` 500s, and the chat UI
shows only a generic "An error occurred" — nothing tells the operator the
encryption key rotated.

Repro loop: boot without `BOW_ENCRYPTION_KEY` → create an LLM provider → kill
and restart only the backend → send any completion. Observed FAIL above;
setting a stable `BOW_ENCRYPTION_KEY` before first boot (and re-saving
credentials) flips it to PASS. Suggested hardening: persist the generated key
(file next to the DB) or refuse to start with encrypted rows it cannot
decrypt, and surface a specific error instead of the generic one.

### 3. Prompt schema hid non-aggregatable text fields (serverless `match_only_text`) — FIXED
Serverless logsdb maps `message`/`error.message` as `match_only_text` with
**no** `.keyword` subfield. `_dtype_for` flattened it to `"string"`, identical
to `keyword`, and the system-prompt rule ("use `message.keyword` for terms
aggs") assumed a keyword multi-field exists. The LLM therefore aggregated on
`error.message` and burned a 29s tool call on ES 400 `match_only_text fields
do not support sorting and aggregations` before recovering.

**Fix:** `_flatten_properties` now annotates analyzed text fields
(`text`/`match_only_text`/`search_as_you_type`) in the dtype the schema
surfaces — `string (full-text; aggregate/sort on <field>.keyword)` when a
keyword subfield exists, `string (full-text; NOT aggregatable/sortable)` when
none does — and the client `description` documents the `runtime_mappings`
fallback. Unit coverage:
`test_analyzed_text_dtype_points_at_keyword_subfield` /
`test_analyzed_text_without_keyword_marked_not_aggregatable`
(`backend/tests/unit/test_elasticsearch_client.py`).

**Observed flip (live):** the same "top 3 error messages with exact counts"
prompt that previously 400'd then retried now completes in a single
`create_data` call — the generated code's own comment reads *"runtime_mappings
to create a keyword version of error.message for aggregation since
error.message is marked as 'NOT aggregatable/sortable' (full-text only)"* and
returns the verbatim messages with exact counts (420/…) directly.
Evidence: `media/es-cloud/24-topmsgs-final-full.png`.

### 4. `POST /api/connections/{id}/test` reports misleading `schema_access: false`
The existing-connection test path (`connection_service.py:741`) only calls
`atest_connection()` and never computes schema access, but the response still
carries `"schema_access": false, "table_count": 0` — for a connection whose
discovery just indexed 8 tables. Harmless but misleading to API consumers;
either populate it (call `_avalidate_schema_access` like the pre-create path
at `connection_service.py:714`) or omit the fields.

### 5. Transient `MissingGreenlet` on provider create-after-delete
`DELETE /api/llm/providers/{id}` followed immediately by
`POST /api/llm/providers` 500'd once with `sqlalchemy.exc.MissingGreenlet`
(sync lazy-load during the async request); an identical retry succeeded.
Not reduced to a deterministic loop — noted for whoever next touches
`LLMService.create_provider`/`delete_provider` session handling.

## Observation on answer quality (not a tool bug)
The generated RCA doc summarized severity as "92 backend errors, 91 frontend
cascade errors" — those are error-*minutes* from the minute-level aggregation,
conflated with error counts (actual: 420/300). When asked explicitly for exact
counts the agent produced the right numbers (420/421). Worth remembering when
evaluating small-model RCA output: the tooling was correct; the prose glossed.

## What this proves / regression notes
The Elasticsearch connector works end-to-end against a real serverless
project: ApiKey auth, full-URL host config, data-stream discovery, DSL search +
aggregations through `create_data`, raw sampling through `inspect_data`, and
the agent's error-recovery loop on connector-surfaced ES errors. Findings 1–2
are pre-existing platform issues unrelated to the connector (hit while wiring
the live environment); 3–4 are connector/API polish items; none blocked the
loop once worked around.
