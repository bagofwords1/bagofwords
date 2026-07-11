# Feedback Loop — Splunk connector (new)

Validates the new Splunk data source connector end-to-end: schema-driven admin
form → connection → live schema discovery (`index::sourcetype` catalog +
capped field sampling) → agent table selection → LLM prompts building
log-investigation charts from real Splunk data, **including the schema-on-read
thin-tail case**. Design doc: `docs/design/splunk-connector.md`.

Splunk is modeled on the **Zabbix** connector (HTTP API, curated catalog,
best-effort enrichment), adapted to Splunk's schema-on-read model. It is an
**enterprise** data source (`requires_license="enterprise"`, gated via
`ENTERPRISE_DATASOURCES`).

## What was added

- `backend/app/data_sources/clients/splunk_client.py` — REST client over the
  management port. Tables are `index::sourcetype`, enumerated with ONE cheap
  `| tstats count where index=* by index, sourcetype` (tsidx metadata, not raw
  events) — O(1) searches regardless of sourcetype count. Fields are sampled
  (`… | head 500 | fieldsummary`) only for the **top-K sourcetypes by volume**
  (`max_sampled_sourcetypes` cap); the rest stay **thin** and are discovered on
  demand. Sampling is bounded, cached, and best-effort (a failure degrades that
  table to thin — never fails discovery). SPL runs as an `exec_mode=oneshot`
  job (no polling). Auth: token (Bearer) / username-password (basic).
- `SplunkConfig` / `SplunkTokenCredentials` / `SplunkUserPassCredentials` in
  `backend/app/schemas/data_sources/configs.py`; registry entry (type `splunk`,
  explicit `client_path`, default auth `token`, `requires_license="enterprise"`)
  in `backend/app/schemas/data_source_registry.py`; `splunk` added to
  `ENTERPRISE_DATASOURCES` in `backend/app/ee/license.py`.
- `frontend/public/data_sources_icons/splunk.png`.
- `backend/tests/unit/test_splunk_client.py` (18 tests).
- `tools/splunk/` — reproducible local environment: `docker-compose.yaml`
  (Splunk 9.3, single container), `seed_splunk.py` (13k events via HEC across
  5 sourcetypes with deliberately different field shapes; prints an auth token).

## Loop A — deterministic reproduction (no external services)

Mock the search boundary; runs in a clean sandbox with no Splunk server.

```bash
cd backend
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('splunk'))"
# -> <class 'app.data_sources.clients.splunk_client.SplunkClient'>
uv run pytest tests/unit/test_splunk_client.py -q
# -> 18 passed
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
# -> 11 passed
```

The unit tests assert the invariants: URL normalization, Bearer vs basic auth,
SPL normalization, the **top-K cap leaving the tail thin** (only the top-K
sourcetypes trigger a sample search; thin tables carry a "discover fields
first" description), field dtype inference, **best-effort enrichment** (a sample
failure degrades to thin, discovery still succeeds), on-demand `get_schema`
sampling, envelope/limit/time handling, and error surfacing.

## Loop B — live confirmation (real Splunk 9.3.14)

```bash
cd tools/splunk
docker compose up -d                    # Splunk 9.3: web :8000, HEC :8088, mgmt :8089
python3 seed_splunk.py                   # 13k events; prints SPLUNK_TOKEN=<token>
```

Then, driving the real `SplunkClient` with `max_sampled_sourcetypes=2`:

```
test_connection            -> {'success': True, 'message': 'Connected to Splunk 9.3.14'}
get_schemas()              -> 5 index::sourcetype tables, ranked by volume:
  web::access_combined       cols=16  sampled=True   (~6000 events)
  app::log4j                 cols=14  sampled=True   (~3000 events)
  app::json_app              cols=0   THIN           (~2500 events)   <- beyond cap
  security::auth_audit       cols=0   THIN           (~1000 events)   <- beyond cap
  metrics::collectd          cols=0   THIN           (~500 events)    <- beyond cap
thin-tail discovery        -> `... json_app | head 1000 | fieldsummary` reveals
                              service, level, latency_ms, user_id, trace_id, internal_error
stats on thin sourcetype   -> `... json_app level IN (error,fatal) | stats count by service`
                           -> billing 350, cart 333, orders 317   (schema-on-read works)
UNKNOWN field              -> `... json_app nonexistent_field=oops | stats count` -> count=0
                              (silently matches nothing — the schema-on-read risk, documented)
5xx timechart (sampled)    -> `index=web sourcetype=access_combined status>=500 | timechart span=1d count`
                           -> ~250-320 5xx/day over 7 days
```

### Full-app agentic pass (the running product)

With the stack booted, an enterprise license (`BOW_LICENSE_KEY`) active, an
Anthropic LLM configured, and the connection created through the catalog:

- Admin catalog shows **Splunk** (icon, enterprise) → connect form → **Test
  connection: "Connected to Splunk 9.3.14"** → tables selector lists all 5
  `index::sourcetype` tables (top-2 with sampled fields, 3 thin).
- Backend log confirms `construct_client: Resolved ClientClass=SplunkClient`
  and `create_data.viz_infer got_raw=True`.
- Prompt against the SAMPLED web sourcetype (HTTP 5xx over time) → timechart.
- Prompt against a THIN sourcetype (`app::json_app`) exercises the
  unknown-schema path and now completes **autonomously**: `describe_tables`
  samples a thin table's fields on inspection (see below), so the agent sees
  real columns and goes straight to `Created Data app::json_app` → bar chart,
  with no clarifying question. Screenshots under `media/es-splunk/`.

### Making thin tables work end-to-end (`describe_tables`)

The thin-tail design only pays off if the on-demand field discovery actually
reaches the agent. Two gaps surfaced and were fixed in
`app/ai/tools/implementations/describe_tables.py` (connector-agnostic):

- **Sample-if-empty:** when an inspected table has zero columns (a thin,
  schema-on-read table), `describe_tables` now calls the client's
  `get_schema(table)` to sample fields live and folds them into the excerpt —
  so the agent inspects the table and *sees the fields*, instead of reading
  "0 columns" as "empty" and asking the user. Best-effort, capped, and a no-op
  for normal sources (their tables already carry columns).
- **Separator-tolerant name matching:** a plain query like `security` now
  matches the collapsed pattern table `security-*` (Elasticsearch) and `web`
  matches `web::access_combined` (Splunk `index::sourcetype`), where the old
  `$`-anchored matcher returned nothing.

## What this proves / regression notes

- The connector resolves via the registry, is enterprise-gated, enumerates its
  `index::sourcetype` catalog cheaply (O(1) searches), samples fields only for
  the top-K sourcetypes (reindex stays bounded), leaves the tail thin, and the
  agent still queries thin sourcetypes correctly by discovering fields on
  demand — the schema-on-read cost model working as designed.
- The unit suite survives as a regression test (mocked boundary, no network).
- Same pre-existing, unrelated `LLMService.create_model` AttributeError and
  benign OpenTelemetry context log lines noted in the Elasticsearch loop.
