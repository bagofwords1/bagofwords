# Feedback Loop — "Endpoint Path Injection in OpenSearch execute_query via unsanitized index"

The report (AI pentest, Medium) said the `index` value in an OpenSearch query is
interpolated into the request path unchecked, so a query can call non-search
APIs. The `index` value is LLM-authored, so prompt injection can control it.

## Root cause (validated)
- `backend/app/data_sources/clients/opensearch_client.py` `execute_query` built
  `f"/{index}/_search"` after only checking `index` is a non-empty string.
- `get_schema` built `f"/{index_name}/_mapping"` the same way.
- `index = "orders/_delete_by_query#"` → `requests` drops the `#…` fragment →
  `POST /orders/_delete_by_query` with our `{"query": …}` body, which is a valid
  delete-by-query body. This also bypassed `SEARCH_KEYS` and `MAX_RESULT_WINDOW`.
- Not affected: the `sql` path (separate endpoint); discovery `index_pattern`
  (admin-configured connection field, not query input).

## Loop A — deterministic reproduction (no external services)
```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run pytest tests/unit/test_opensearch_client.py -q -k Injection
```
Before the fix: `20 failed, 7 passed`. Each malicious index (`orders/_delete_by_query?…`,
`../_cluster/settings`, `orders?pretty`, `orders%2F…`, `orders#…`, whitespace, `\`) reached
the transport. Legitimate expressions (`logs-*,metrics-*`, `remote:orders`,
`-excluded,orders*`, `orders-2026.10.11`) already passed.

## Loop B — live confirmation (local OpenSearch, no credentials)
```bash
docker run -d -p 9200:9200 -e discovery.type=single-node -e DISABLE_SECURITY_PLUGIN=true \
  opensearchproject/opensearch:2.17.1
```
The script seeds 5 docs in `orders`, then calls
`OpenSearchClient(host="localhost", port=9200).execute_query('{"index": "orders/_delete_by_query#", "query": {"match_all": {}}}')`.

| | result | docs before → after |
|---|---|---|
| before fix | accepted | **5 → 0** (data deleted) |
| after fix | `ValueError: Invalid index expression` | 5 → 5 |

## The fix
`OpenSearchClient._check_index` allows only one path segment of index-expression
characters `[A-Za-z0-9_.*,:+-]`. It rejects `.`/`..` segments, and it runs before
any request in both `execute_query` and `get_schema`.
Loop A after the fix: `69 passed` (whole file).

## What this proves / regression notes
The parametrized tests check the general rule: no index value can reach the
network unless it is a single safe segment, and real index expressions still
route to `/{index}/_search`.
Defense in depth that's still worth doing: recommend read-only cluster credentials
for OpenSearch connections.
