# Object Store connector (AWS S3) — design

A new file-based data source that exposes an **AWS S3 bucket (or a prefix within
it)** to the agent as a browsable, readable file catalog. Named "Object Store"
in the product with a provider dimension (AWS first; GCS / Azure Blob later), it
reuses the existing `data_shape="files"` stack end-to-end — `list_files`,
`read_file`, the catalog index, `attach_file` — so it ships **no new agent tool
surface**.

Status: design. Target branch: `claude/s3-connector-design-kjgkkn`.

## Principle

**An object store is just a remote filesystem to the agent.** It conforms to the
existing files contract (`base.py` — `list_files` / `read_file` / entry shape),
so the generic file tools drive it with zero per-source code. The only genuinely
new behavior is **ranged (cursor) reads**, because objects are frequently far too
large to load whole.

## Scope

**In (v1, AWS S3):**
- `LIST_FILES` — enumerate objects under a bucket + prefix (with sizes, types).
- `READ_FILE` — read an object, in two modes:
  - **structured** (no range): parse a bounded object into DataFrame / text /
    extracted document text, attach as a session file — identical to
    `network_dir` today.
  - **windowed / cursor** (with `offset`/`length`): return a byte window plus a
    cursor to page forward — for large logs / ndjson / CSV-as-text.
- Catalog indexing via `get_schemas()` (keywords per object, like `network_dir`).
- Confinement to the configured prefix; test-connection; real-bucket testing.

**Out (v1):**
- `WRITE_FILE` (put_object) — deferred; the class *may* advertise it later behind
  a `writable` flag exactly like `network_dir`.
- `SEARCH_FILES` live content scan — deferred. On an object store this is a
  `GET`-per-object and is cost/latency-hostile. Keyword search over the indexed
  catalog (built in `get_schemas`) covers the common case with no live scanning.
- GCS / Azure Blob providers — the client and registry are shaped to add them as
  sibling auth variants, but only the S3 adapter lands in v1.

## Why `data_shape="files"`, not `"objects"`

`data_shape="objects"` is already taken — and it means **document databases**
(Elasticsearch, MongoDB, OpenSearch: collections of JSON documents), not blob
storage. Our connector wants the *file* catalog + file tools, so it is
`data_shape="files"`, `is_document_based=True`, `catalog_ownership="shared"`
(an admin points at one bucket/prefix that is the single source of truth for
everyone — same ownership model as `network_dir` and a SharePoint library). The
"Object Store" name is a UX label, not an internal shape.

## Registry entry

`backend/app/schemas/data_source_registry.py`:

```python
"object_store": DataSourceRegistryEntry(
    type="object_store",
    title="Object Store",
    description=(
        "Browse and read files from a cloud object store (Amazon S3). "
        "Reads inside PDF, Word, PowerPoint, Excel and CSV, and can attach "
        "objects to a report. Large objects can be read in byte-range windows."
    ),
    config_schema=ObjectStoreConfig,
    credentials_auth=AuthOptions(
        default="aws_keys",
        by_auth={
            # The auth-variant dropdown doubles as the provider/credential
            # picker. GCS / Azure variants slot in here later.
            "aws_keys":    AuthVariant(title="AWS Access Key",       schema=S3KeyCredentials,     scopes=["system"]),
            "aws_role":    AuthVariant(title="AWS Assume Role (STS)", schema=S3RoleCredentials,    scopes=["system"]),
            "aws_default": AuthVariant(title="AWS Default Chain",     schema=S3DefaultCredentials, scopes=["system"]),
        },
    ),
    client_path="app.data_sources.clients.object_store_client.ObjectStoreClient",
    is_document_based=True,
    data_shape="files",
    catalog_ownership="shared",
    ui_form="data_source",
    version="beta",
    # Community / open tier — a bucket is treated like a plain directory
    # (same as network_dir, which is ungated). No requires_license set.
)
```

## Config & credential schemas

`backend/app/schemas/data_sources/configs.py` — credentials mirror the Athena
idiom (`AWSAthenaCredentials` / `AWSAthenaDefaultCredentials`) so the boto3
session construction is familiar and reusable.

```python
class S3KeyCredentials(BaseModel):
    access_key: str = Field(..., title="Access Key",  json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key",  json_schema_extra={"ui:type": "password"})
    session_token: Optional[str] = Field(None, title="Session Token", json_schema_extra={"ui:type": "password"})

class S3RoleCredentials(BaseModel):
    access_key: str = Field(..., title="Access Key", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key", json_schema_extra={"ui:type": "password"})
    role_arn:   str = Field(..., title="Role ARN",   json_schema_extra={"ui:type": "string"})

class S3DefaultCredentials(BaseModel):
    """No credentials — boto3 resolves via its default chain (env, instance
    profile, IRSA). Mirrors AWSAthenaDefaultCredentials."""
    class Config:
        extra = "allow"

class ObjectStoreConfig(BaseModel):
    bucket: str = Field(..., title="Bucket",
        description="S3 bucket name.", json_schema_extra={"ui:type": "string"})
    prefix: Optional[str] = Field(None, title="Prefix",
        description="Key prefix to scope the connection (e.g. 'reports/2025/'). "
                    "All listing and reads are confined to this prefix. Blank = whole bucket.",
        json_schema_extra={"ui:type": "string"})
    region: Optional[str] = Field(None, title="Region",
        description="AWS region of the bucket (e.g. 'us-east-1').", json_schema_extra={"ui:type": "string"})
    endpoint_url: Optional[str] = Field(None, title="Endpoint URL",
        description="Custom S3 endpoint for S3-compatible stores (MinIO / R2 / Wasabi). "
                    "Leave blank for AWS.", json_schema_extra={"ui:type": "string"})
    allowed_extensions: Optional[str] = Field(None, title="Allowed Extensions",
        description="Comma-separated extensions to include (e.g. 'xlsx,csv,pdf'). Blank = all.",
        json_schema_extra={"ui:type": "string"})
    recursive: bool = Field(True, title="Include Sub-prefixes",
        json_schema_extra={"ui:type": "boolean"})
    max_file_mb: int = Field(100, title="Max File Size (MB)",
        description="Reject structured reads above this size (windowed reads are exempt).",
        json_schema_extra={"ui:type": "number"})
    max_catalog_objects: int = Field(5000, title="Max Catalog Objects",
        description="Cap on objects indexed into the catalog to keep get_schemas bounded.",
        json_schema_extra={"ui:type": "number"})
```

## Client

`backend/app/data_sources/clients/object_store_client.py` — new
`ObjectStoreClient(DataSourceClient)`. Structurally a sibling of
`network_dir_client.py`; the filesystem primitives are swapped for boto3 calls,
and **all the document/tabular/keyword machinery is reused verbatim** via the
existing shared helpers (`_document_text`, `_keywords`) and pandas parsing.

Capabilities (class level): `LIST_FILES`, `READ_FILE`. (`WRITE_FILE` reserved
for a later `writable` flag; `SEARCH_FILES` reserved for a later opt-in scan.)

### boto3 session

Reuse the Athena pattern (`aws_athena_client.py:44-60`): build a `boto3.Session`
from the chosen auth variant (keys, keys+STS assume-role, or default chain),
then `session.client("s3", region_name=..., endpoint_url=...)`. Constructed
lazily in `connect()` / on first use so a bad bucket surfaces in
`test_connection()`, not at construction.

### Method map

| DataSourceClient method | S3 implementation |
|---|---|
| `connect()` | build boto3 session + `s3` client (lazy) |
| `test_connection()` | `head_bucket` (or a 1-key `list_objects_v2`) under prefix → `{success, message}` |
| `list_files(folder_id, recursive)` | `list_objects_v2` paginator, `Prefix=` (prefix + folder_id), `Delimiter="/"` when not recursive; `Contents[]`→ entries, `CommonPrefixes[]`→ folder entries |
| `read_file(file_id, sheet, offset, length, max_bytes)` | ranged or whole `get_object`; parse identically to `network_dir` (csv/tsv/xlsx→DataFrame, doc→extracted text, text→str) |
| `read_raw_bytes(file_id)` | `get_object` → `(bytes, name, mime)` for `attach_file` |
| `get_schemas()` | `list_files` → `Table` rows, `metadata_json={"object_store": {...}}`, keyword-index each object (bounded by `max_catalog_objects`) |
| `get_schema` / `prompt_schema` / `execute_query` | same thin wrappers as `network_dir` |

### Entry shape mapping

The standard file entry (`base.py:154`:
`{id, name, path, mime_type, size, modified_at, is_folder, web_url}`) maps
directly onto S3 list output — **size and type included by construction**, which
is the whole point of conforming to the contract:

| entry field | source |
|---|---|
| `id` / `path` | object key relative to the configured prefix (stable, human-readable, round-trips through the LLM) |
| `name` | last path segment of the key |
| `size` | `Contents[].Size` — free, no HEAD |
| `modified_at` | `Contents[].LastModified` (ISO-8601 UTC) |
| `mime_type` | `mimetypes.guess_type(key)` — derived from extension (list responses carry no Content-Type; avoids a HEAD per object) |
| `is_folder` | `True` for `CommonPrefixes` entries |
| `web_url` | `s3://bucket/key` (or console URL) |

**Junk / marker filtering** (same spirit as `network_dir._is_junk`): skip
zero-byte keys ending in `/` (console "folder markers", e.g. `results/`), and
optionally skip sidecar cruft like `*.metadata`. Confirmed necessary against the
real bucket (see validation) — an Athena results prefix is full of these.

## Cursor / position reads

The new capability. `read_file` gains optional `offset` + `length`:

- **absent** → structured read: whole object (guarded by `max_file_mb`), parsed
  and attached as a session file. Existing behavior, unchanged for callers.
- **present** → windowed read: `get_object(Range=f"bytes={offset}-{offset+length-1}")`,
  return the window plus a cursor. No parse, no auto-attach.

Windowed output fields:

```jsonc
{ "content": "...", "encoding": "text" | "base64",
  "offset": 0, "length": 1048576,
  "next_cursor": 1048576, "total_size": 5242880, "eof": false }
```

- `total_size` from `Content-Range` on the ranged response (or one `head_object`);
  `eof = next_cursor >= total_size`.
- **Text is snapped to line boundaries**: for text/ndjson/csv-as-text, trim the
  window back to the last complete newline and set `next_cursor` there, so the
  agent never sees a half-line and pages cleanly. Binary returns base64 with no
  snapping.
- `{"suffix": n}` range (last N bytes) gives a free `tail` for logs — worth
  exposing later, not required for v1.
- **Not for tabular/parquet**: byte offsets cut mid-record. v1 keeps
  csv/xlsx/parquet on the whole-object-under-cap path; row-level pagination is a
  separate future mechanism, not a byte cursor.

### Shared tool-schema change (cross-cutting — do deliberately)

`offset` / `length` / `next_cursor` / `total_size` / `eof` don't exist on the
shared file-tool schemas today (`ReadFileInput` carries only `sheet` /
`max_rows` / `max_chars`; `ReadFileOutput` has no cursor fields —
`file_tools.py:85,123`). Supporting cursor reads means **adding optional fields
to those shared schemas**, which touches every files connector — they're
optional no-ops elsewhere (and `network_dir` could honor them for parity). This
is intentional surface, not something to bury inside the S3 client:

- `ReadFileInput`: add `offset: Optional[int]`, `length: Optional[int]`.
- `ReadFileOutput`: add `next_cursor`, `total_size`, `eof`, and a `content_type`
  value (or flag) for the windowed case.
- `read_file.py` tool: when `offset` is set, pass it through and skip
  `render_file_payload` / `_persist_session_file` (windowed reads aren't parsed
  or attached).

## Security & confinement

- The configured **prefix is the boundary**: every `file_id` is a key *relative*
  to it; on resolve, normalize (`posixpath.normpath`) and reject any key that
  escapes the prefix (`..`, leading `/`, absolute) — the S3 analogue of
  `network_dir._resolve`'s root check.
- No symlink concern (object stores have none), but the same normalize-then-verify
  chokepoint gates every read.
- Credentials live in the encrypted credentials store like every other
  connector; never logged.

## Cost & scale

- **Listing** paginates (`list_objects_v2`, 1000 keys/page); the paginator
  handles continuation tokens. `Delimiter="/"` for non-recursive keeps a large
  bucket browsable folder-by-folder.
- **Cataloging** is capped by `max_catalog_objects` — a million-key bucket must
  not become a million catalog rows. Log when the cap truncates (no silent
  truncation) and lean on `prefix` to scope.
- **No live content search in v1** (see Scope) — the indexed keyword catalog is
  the search path; a bounded opt-in scan can come later.
- Ranged reads keep large-object memory flat — read a window, not the object.

## Dependencies

**None new for v1.** `boto3>=1.43.33,<1.44` is already in `pyproject.toml:47`
(used by Athena / Redshift). GCS (`google-cloud-storage`) and Azure
(`azure-storage-blob`) deps are added only when those provider variants are built.

## Real-bucket validation (target: `bowathena14`)

Read-only connectivity check run against the provided bucket, confirming the
design's assumptions before build:

- **Auth**: static access-key credentials resolve and authorize `list_objects_v2`
  and ranged `get_object`.
- **Region**: bucket is **`us-west-2`**; boto3 auto-redirected the list, but the
  explicit `region` config field is the correct fix (don't rely on redirect).
- **Ranged read**: `Range: bytes=0-199` → HTTP 206 with `Content-Range`, so
  `total_size` / `eof` can be computed from the response header — no separate
  `head_object` needed on the first window.
- **Noise → validates scoping/caps/junk-filtering**: the bucket holds 3 real
  documents (a `.csv`, two `.parquet`) alongside a large Athena `results/` dump —
  hundreds of tiny `.csv` / `.txt` / `.metadata` files, a zero-byte `results/`
  folder-marker key, and a ` ` (single-space) sub-prefix. Confirms we need
  `prefix` scoping, `max_catalog_objects`, junk filtering, and robust key
  normalization (spaces, trailing slashes) at the resolve chokepoint.

Seed a clean `docs/` prefix (pdf/docx/xlsx/csv/large.log) for the acceptance run
so cataloging isn't dominated by the Athena results.

## Testing plan

- **Unit** (`backend/tests/unit/test_object_store_client.py`): stub boto3 with
  `botocore.stub.Stubber` (or `moto`) — list mapping, entry fields, prefix
  confinement / traversal rejection, structured vs windowed read, newline
  snapping, `eof`/`next_cursor` math, oversize rejection.
- **Live bucket** (gated on env creds — provided separately): a small script /
  marked test that connects to a real bucket seeded with sample docs
  (pdf/docx/xlsx/csv/large.log), runs `test_connection` → `list_files` →
  `get_schemas` → structured read → windowed paged read to EOF, and
  `read_raw_bytes` → attach. This is the acceptance gate for the build.
- **E2E**: register an `object_store` connection and confirm `list_files` /
  `read_file` tools resolve and drive it via `resolve_file_client`.

## Provider generalization (later)

The client keeps a tiny internal driver interface — `list(prefix)`,
`get_range(key, offset, length)`, `head(key)` — behind which the S3 adapter
lives. GCS (`list_blobs` + `download_as_bytes(start,end)`) and Azure
(`walk_blobs` + `download_blob(offset,length)`) become sibling adapters selected
by the auth variant, with no change to the catalog / tool layers. Ship S3, prove
the cursor design against a real bucket, then add the other two.

## File-by-file change list

1. `backend/app/schemas/data_sources/configs.py` — `ObjectStoreConfig`,
   `S3KeyCredentials`, `S3RoleCredentials`, `S3DefaultCredentials`.
2. `backend/app/schemas/data_source_registry.py` — `"object_store"` entry.
3. `backend/app/data_sources/clients/object_store_client.py` — `ObjectStoreClient`.
4. `backend/app/ai/tools/schemas/file_tools.py` — optional cursor fields on
   `ReadFileInput` / `ReadFileOutput`.
5. `backend/app/ai/tools/implementations/read_file.py` — windowed-read branch
   (pass through offset/length; skip parse + attach).
6. `backend/tests/unit/test_object_store_client.py` — unit tests.
7. Frontend: `object_store` picks up the generic `data_source` create form via
   the registry (config + credential schemas render automatically); confirm the
   auth-variant dropdown renders the provider/credential options. No bespoke UI
   expected for v1.

## Open questions

1. ~~**License tier**~~ — **resolved: community / open** (no `requires_license`),
   treated like `network_dir`.
2. **Windowed default `length`** — pick a sane page size (e.g. 1 MiB) and a max.
3. **`content_type` for windowed reads** — new enum value vs a `windowed` flag on
   `ReadFileOutput`.
