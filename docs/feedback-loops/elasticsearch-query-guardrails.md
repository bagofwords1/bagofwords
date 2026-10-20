# Elasticsearch query guardrails + per-connection query limits

An admin connecting a production Elasticsearch cluster needs to bound what the
agent may ask of it: no `GET /_search` across every index, a cap on documents
and aggregations per query, and a timeout and concurrency cap that are set **on
that connection**. Before this pass, none of the Elasticsearch caps existed. The
timeout and concurrency overrides were read from connection config, but nothing
in the product could set them. The Elasticsearch engine timeout was also
hard-coded to 60s, whatever the organization configured.

Everything below ran against a real Elasticsearch 8.15.3 (Docker), the full
BOW stack (backend + Nuxt dev), and real browser sessions (Playwright) for an
admin and a plain member.

## Environment

```bash
dockerd &
docker run -d --name es -p 9200:9200 -e discovery.type=single-node \
  -e xpack.security.enabled=true -e xpack.security.http.ssl.enabled=false \
  -e ELASTIC_PASSWORD=bowtest123 -e "ES_JAVA_OPTS=-Xms1g -Xmx1g" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.3
curl -u elastic:bowtest123 -XPUT localhost:9200/_cluster/settings -H 'Content-Type: application/json' \
  -d '{"persistent":{"cluster.routing.allocation.disk.threshold_enabled":false}}'
```

Seeded `applogs-2026.10.12` + `applogs-2026.10.13` (3,000 log docs each:
service / level / host / status / latency_ms) and `finance-secret` (1 doc), an
index the agent must never reach. Do not name test indices `logs-*-*`: ES 8's
built-in template turns them into data streams.

Stack: `tools/agent/boot_stack.sh --dev`, `tools/agent/seed_org.py --invite
member@example.com`, an Elasticsearch connection "Prod Logs (Elastic)" and an
agent "Ops Logs" on it with `applogs-*` activated.

**LLM.** The Anthropic key supplied for this run was rejected (`401 invalid
x-api-key`), and the sandbox key had no credit. The agent run was driven by
`tools/agent/stub_llm_es_guardrails.py`, a scripted OpenAI-compatible model
registered as an `openai` provider with `base_url http://127.0.0.1:9098/v1`.
It plays an agent that first asks for all indices, then for an oversized
aggregation, and then complies. Everything downstream of the model is
production code: planner, create_data, codegen retry loop, executor, wrapper,
client and cluster.

**Backend gotcha.** `main.py` always runs uvicorn with `reload=True`. Editing a
file under `app/` mid-session made the reload hang (`/health` timed out). Run
`uvicorn main:app --port 8000` without reload when you'll be editing.

## Layer 1: the agent's data path against the real cluster

`tools/agent/es_guardrails_probe.py` builds clients exactly as
`create_data`/`inspect_data` do (`construct_clients` → `wrap_clients_for_capture`)
and runs a query matrix against the cluster.

| Query | Base code | Defaults (no admin config) | Admin limits set in the UI¹ |
|---|---|---|---|
| `applogs-*` search | ✅ | ✅ | ✅ |
| `"index": "*"` (GET /_search) | ✅ **reads everything** | ⛔ never reaches ES | ⛔ |
| `"index": "_all"` | ✅ | ⛔ | ⛔ |
| `*` + term on `account` → **finance-secret doc** | ✅ **leaks** | ⛔ | ⛔ |
| `finance-secret` by name | ✅ | ✅ | ⛔ outside allowlist |
| ES\|QL `FROM *` / SQL `FROM "*"` | ✅ | ⛔ / ⛔ | ⛔ / ⛔ |
| `size: 5000` | ✅ 5000 rows | ✅ | ⛔ cap 1000 |
| ES\|QL `LIMIT 5000` | ✅ 5000 rows | ✅ | ✅ **1000 rows** (LIMIT appended) |
| 1 terms aggregation | ✅ | ✅ | ✅ |
| 4 aggregations, 3 levels deep | ✅ | ✅ | ⛔ count + depth |
| terms(host, 5000) > terms(status, 20) | ✅ | ✅ | ⛔ ~105,000 buckets |
| terms `size: "lots"` (unreadable) | ES 400 | ES 400 | ES 400: **our checker failed open** and the cluster rejected it itself |
| agent sets `timeout: "10m"` | sent as **10m** | clamped to 180s (org) | clamped to **20s** |
| engine timeout / HTTP read | 60s / 65s fixed | 180s / 185s | 20s / 25s |

¹ allowed targets `applogs-*`, max docs 1000, max aggregations 3, max depth 2,
max buckets 1000, query timeout 20s, concurrent queries 2.

**Concurrency** (cap 2, 6 parallel distinct slow aggregations): peak in flight
at the cluster was **2**, and the queries finished in waves (1.5s, 1.6s, 2.6s,
3.5s, 4.3s). Make each query distinct, or ES's request cache answers the
repeats instantly and hides the queueing.

**Timeout.** With the connection's timeout at 1s, a ~2s runtime-field
aggregation raised `QueryTimeoutError` after **1.0s**. Sent straight to the
cluster with `timeout: 200ms`, the same query answers **HTTP 200,
`timed_out: true`, 4,096 of 6,000 docs counted**: a partial number that reads
as a real one. The client now refuses that: *"Elasticsearch timed out after 1s
and returned partial results only."*

### Holes found in self-review, reproduced on the cluster, then closed

| Bypass | On the real cluster before the fix | Now |
|---|---|---|
| `"index": "%2A"` | ES decodes it to `*`: the **finance-secret doc is returned** | ⛔ invalid index target (index names can't contain `/ \ ? # % " < > \|` or spaces) |
| `applogs-x/../finance-secret` | matched the `applogs-*` glob | ⛔ same rule |
| ES\|QL `// comment\nFROM *` | ran: the target check only looked at the first token | ⛔ comments are stripped before the source is read |
| SQL `EXTRACT(HOUR FROM "@timestamp")` with an allowlist set | `@timestamp` was read as a target, so a valid query was **refused** | ✅ FROM inside a function call or a string literal isn't a target; a subquery's FROM still is |
| SQL on an uncapped connection | the first draft always sent `fetch_size: 10000` | engine default unless the admin set a cap |

## Layer 2: real browser, real users

* **Admin**: Agents → Connections → Manage connection. The new **Query limits**
  panel shows the org defaults as placeholders ("Default (180)", "Default (4)");
  setting 20 / 2 saves through `PUT /api/connections/{id}`. The Elasticsearch
  form shows the guardrail fields; **Save changes** sends the form's config
  without the limit keys, and the server keeps them (verified with GET after
  the save).
* **Member**: no "Manage connection" link, and `PUT` with limits → **403**.
* **Member, agent chat** ("Which services had the most errors? Check every
  index."): attempt 1 was refused (*all indices blocked → target one of
  applogs-\**), attempt 2 was refused (*4 aggregations (limit 3); 3 nested
  levels (limit 2)*). The agent re-planned within the limits and returned real
  counts for 6 services.
* **Member, report code editor** (same executor as the agent): `finance-secret`
  → outside allowlist; `size: 5000` → doc cap; ES|QL `FROM *` → blocked; a
  compliant terms aggregation → 5 rows.
* **Hebrew (RTL)**: the panel mirrors correctly.

Screenshots: `media/pr/ai-tender-heisenberg-on6rdf/`.

## Layer 3: tests

* `tests/unit/test_elasticsearch_guardrails.py`: 59 cases through
  `execute_query` with the HTTP boundary faked. With fail-open and the
  all-index check broken on purpose, 13 fail.
* `tests/unit/test_query_timeout.py`: the wrapper hands its budget to
  Elasticsearch.
* `tests/e2e/test_connection_query_limits.py`: set/clear/validate/RBAC, plus
  overrides surviving a connection-form save. With the preserve logic removed,
  that test fails.
