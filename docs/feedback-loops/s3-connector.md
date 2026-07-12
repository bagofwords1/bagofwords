# Feedback Loop — "we have network and files connectors; do the same with S3"

Add an **Amazon S3** data source (`type="s3"`) that exposes a bucket (optionally
scoped to a key prefix) to the agent as a browsable, readable file catalog —
reusing the existing `data_shape="files"` stack (`list_files` / `read_file` /
catalog index / `attach_file`) with **no new agent tool surface** — plus a new
**windowed byte-range ("cursor") read** for objects too large to load whole.

This loop proves the connector lists, indexes, reads (structured + windowed), and
confines to its prefix — both deterministically (stubbed AWS) and live (a real
bucket).

## What was built (file:line)

- `backend/app/data_sources/clients/s3_client.py` — `S3Client`, a files-shape
  client (boto3). Declares `LIST_FILES` + `READ_FILE`; write and live search are
  out of scope for v1.
- `backend/app/schemas/data_sources/configs.py` — `S3Config` +
  `S3KeyCredentials` / `S3RoleCredentials` / `S3DefaultCredentials` (mirrors the
  Athena auth idiom).
- `backend/app/schemas/data_source_registry.py` — the `"s3"` registry entry
  (`data_shape="files"`, `catalog_ownership="shared"`, community tier; the
  auth-variant dropdown doubles as the credential picker).
- Files-tool wiring: `s3` added to `FILE_SOURCE_TYPES`
  (`_file_tool_common.py:25`) and to `_FILE_METADATA_KEYS` in `list_files.py:29`
  and `search_files.py:31` so the catalog-backed tools find the client's
  `metadata_json={"s3": ...}`.
- Cursor read: optional `offset`/`length` on `ReadFileInput` and
  `next_cursor`/`total_size`/`eof`/`encoding`/`windowed` on `ReadFileOutput`
  (`file_tools.py`), with a windowed branch in `read_file.py` that returns the
  raw byte window and skips parse/attach.
- `frontend/public/data_sources_icons/s3.svg` + map in `DataSourceIcon.vue`.

## Root cause / design (validated)

An object store is just a remote filesystem to the agent: conform to the files
contract (`base.py:154` entry shape `{id,name,path,mime_type,size,modified_at,
is_folder,web_url}`) and the generic file tools drive it. The only genuinely new
behavior is **ranged reads**, because objects (logs/ndjson/big CSVs) are
frequently too large to load whole — S3 supports `GET ... Range: bytes=a-b`
natively (HTTP 206 + `Content-Range` giving total size).

`data_shape="files"`, NOT `"objects"` — the latter is already used for *document
databases* (Elasticsearch/Mongo/OpenSearch), a different shape.

## Loop A — deterministic (stubbed AWS, no creds)

`backend/tests/unit/test_s3_client.py` stubs the AWS boundary with botocore's
`Stubber` (no `moto` dependency, no network). Covers registry wiring +
capabilities, prefix confinement / traversal rejection, list mapping
(size/type/`CommonPrefixes`→folders/junk filtering), structured csv read, oversize
rejection, and the windowed read (cursor math + newline snapping + base64 for
binary).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('s3'))"
# -> <class 'app.data_sources.clients.s3_client.S3Client'>
uv run pytest tests/unit/test_s3_client.py -q
# -> 20 passed
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
# -> 11 passed   (generic create/update/delete/test-connectivity flows still green)
```

## Loop B — live confirmation (real bucket, creds via env)

Against a real bucket (`bowathena14`, `us-west-2`). Credentials are passed as env
vars only — never committed, never echoed. The harness
(`scratchpad/live_loop.py`, not committed) seeds a clean `docs/` prefix then
drives the client end to end.

```bash
cd backend
export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… AWS_CA_BUNDLE=/root/.ccr/ca-bundle.crt
uv run python /path/to/live_loop.py
```

Observed (abridged):

```
[test_connection] {'success': True, 'message': 'Connected — objects visible under s3://bowathena14/docs'}

[list_files]
  events.log    size=6800  type=None
  pnl.xlsx      size=5418  type=…spreadsheetml.sheet
  revenue.csv   size=42    type=text/csv
  team.json     size=36    type=application/json

[get_schemas — keyword index]
  revenue.csv   kw=['revenue','csv','region','emea','amer','apac']   # keywords from CONTENT
  pnl.xlsx      kw=['pnl','xlsx','sheet','quarter','profit']

[structured read: revenue.csv]  -> DataFrame (3, 2)
[structured read: pnl.xlsx]     -> DataFrame cols ['quarter','profit']

[windowed read: events.log — page @120 bytes to EOF]
  page 1:  off=0    -> next=102  bytes=102 eof=False total=6800   # snapped to line (3×34B)
  …
  page 67: off=6732 -> next=6800 bytes=68  eof=True  total=6800
  paged 67 windows, 200 lines total (expected 200)               # every window newline-terminated

[read_raw_bytes: pnl.xlsx]  name=pnl.xlsx mime=…sheet bytes=5419
[confinement] read_file("../secret") -> ValueError: Key escapes the connection prefix
```

## What this proves

- Auth (static keys) → `list_objects_v2` + ranged/whole `get_object` authorize.
- List returns **size + type** by construction; content-derived **keyword
  indexing** works over csv/xlsx/json/log.
- **Structured** reads parse csv/xlsx → DataFrame (identical to `network_dir`).
- **Windowed** reads page cleanly to EOF with correct `next_cursor`/`eof`/
  `total_size`, and every text window is newline-snapped (no half-lines).
- **Prefix confinement** rejects traversal at the single `_resolve_key`
  chokepoint.

## Regression notes

- `events.log` surfaces `mime_type=None` — `mimetypes` has no mapping for `.log`;
  the object still reads as text. Cosmetic, matches `network_dir` behavior.
- The real bucket also holds a noisy Athena `results/` dump (folder-marker keys,
  `*.metadata` sidecars, a space sub-prefix) — filtered by `_is_junk`; the
  acceptance run scopes to a clean `docs/` prefix so indexing isn't dominated by
  it. This is exactly why `prefix` scoping + `max_catalog_objects` exist.
- Not yet exercised: STS assume-role and default-chain auth variants (only static
  keys were available); live search (deferred by design); write (out of scope).

## Not done here (deliberate, per design)

`WRITE_FILE`, live `SEARCH_FILES` content scan, and GCS/Azure providers — see
`docs/design/s3-object-store-connector.md`. The client keeps a thin internal I/O
surface so the other two providers can slot in as sibling adapters later.
