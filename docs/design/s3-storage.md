# S3 storage backend — design map (not implemented)

Status: **mapping only**. Nothing here is built yet.

## Why

Every durable file the app writes goes to `backend/uploads/` on local disk
(52 backend modules reference it). That works for one container with a volume,
but:

- **Helm chart has no uploads volume.** `k8s/chart/templates/deployment.yaml`
  mounts only `bow-config.yaml` and certs; uploads live in the container's
  writable layer unless an operator adds `extraVolumes`. A pod restart loses
  every uploaded file, thumbnail, PDF export and avatar, while the DB rows that
  point at them survive.
- **No horizontal scaling.** Two replicas = two disjoint `uploads/` dirs. A file
  uploaded on pod A 404s on pod B. RWX volumes (EFS/NFS) are the only workaround
  today.
- Customers on AWS/GCP/MinIO expect object storage, lifecycle rules, and backups
  outside the app container.

## The core constraint: much of the app needs a *local path*

This is what makes S3 more than "swap `open()` for `put_object`":

| Consumer | Needs | Where |
|---|---|---|
| Code sandbox | real path in `excel_files[i].path`; landlock allow-lists dirs | `ai/code_execution/code_execution.py:450-583`, `sandbox/runner.py:198`, `sandbox/landlock.py` |
| Preview generation | `file.path` → pandas/openpyxl/pdf readers | `services/file_preview*.py:57` |
| File tools (read/grep/excel→csv) | local reads | `ai/tools/implementations/_file_tool_common.py`, `read_excel_as_csv.py:302`, `grep_files.py` |
| LLM context | `File.path` string printed into prompts | `models/file.py` (`description`) |
| PDF/thumbnail render | Playwright/LibreOffice write to a file | `services/report_pdf_service.py`, `thumbnail_service.py`, `_office_convert.py` |
| Git repos | working trees | `services/repo_cache.py`, `git_hardening.py` |

So the design is **object store = source of truth, local disk = cache**. Any
consumer that needs a path calls `storage.materialize(key) -> local Path`, which
downloads into a node-local cache dir on miss.

## Proposed abstraction

`backend/app/services/storage/` (new):

```python
class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes | AsyncIterator[bytes], content_type: str | None = None) -> None
    async def get(self, key: str) -> bytes
    async def open_stream(self, key: str) -> AsyncIterator[bytes]   # for StreamingResponse
    async def exists(self, key: str) -> bool
    async def delete(self, key: str) -> None
    async def delete_prefix(self, prefix: str) -> None              # e.g. release-{id}/
    async def copy(self, src: str, dst: str) -> None                # dashboard copy, thumbnail copy
    async def materialize(self, key: str) -> Path                   # local path (no-op for LocalBackend)
    async def presigned_url(self, key: str, expires_s: int) -> str | None  # None for local
```

- `LocalBackend` — today's behavior; root = `uploads/`. `materialize` returns
  the real path. Default, zero migration.
- `S3Backend` — `boto3` is **already a dependency** (`pyproject.toml:51`).
  Wrap sync calls in `asyncio.to_thread` (or add `aioboto3`). Supports
  `endpoint_url` for MinIO/R2/GCS-interop.
- Keys are the existing relative paths minus the `uploads/` prefix
  (`files/<uuid>_<name>`, `thumbnails/<id>.png`, …) so the DB `File.path`
  format barely changes.
- Path-traversal guards (`safe_join`, `realpath` checks in routes) become key
  validation in one place.

## Config

New section in `settings/bow_config.py` + `bow-config.yaml`:

```yaml
storage:
  backend: local            # local | s3
  local:
    root: uploads
  s3:
    bucket: ""
    prefix: "bow/"          # optional key prefix
    region: ""
    endpoint_url: ""        # MinIO / R2 / custom
    access_key_id: ""       # empty → default AWS chain (IRSA, instance role)
    secret_access_key: ""
    sse: ""                 # "", AES256, aws:kms
    kms_key_id: ""
    presign_downloads: false
  cache:
    dir: uploads/.cache     # node-local, ephemeral is fine
    max_bytes: 5GB
```

Env overrides `BOW_STORAGE_BACKEND`, `BOW_S3_BUCKET`, etc., matching the
`BOW_DATABASE_*` pattern.

## Inventory — what to migrate

### A. Durable user/app data → must go to S3

| Data | Write site(s) | Read/serve site(s) |
|---|---|---|
| User uploads | `services/file_service.py` `upload_file`, `save_bytes_as_file` | `routes/file.py:214-255`, previews, sandbox |
| Tool-produced files | `_file_tool_common.py:1042-1099`, `attach_file.py:153`, `write_csv.py:156,336`, `execute_mcp.py:170-184` | same as uploads |
| Chat-platform attachments | `platform_adapters/{slack,teams,whatsapp,email,google_chat}_adapter.py` (via `save_bytes_as_file`) | — |
| Thumbnails | `services/thumbnail_service.py:19-191` | `routes/branding.py:48` `/thumbnails/{filename}`, `dashboard_list.py:56` |
| Report/artifact PDFs | `services/report_pdf_service.py:367-1118` | `routes/report.py:896`, `routes/artifact.py:632`, `email_send_service.py:654` |
| PPTX + slide previews | `create_artifact.py:1451,1692`, `edit_artifact.py:364`, `edit_artifact_legacy.py:1585`, `pptx_executor.py:320` | `routes/artifact.py:796`, `routes/dashboard.py:700` |
| Precompiled artifact JS | `services/artifact_precompile.py:156` | artifact/embed routes |
| Branding (logos) | `organization_settings_service.py:532,585` | `routes/branding.py` |
| Avatars | `routes/user_profile.py:363-402` | `/api/users/avatar/` |
| Demo data | `services/demo_data/installer.py:36` | demo CSV/duckdb clients |
| Dashboard copy / lifecycle | `services/dashboard_copy.py:50,499,543`, `dashboard_lifecycle.py:299` | — (needs `copy` + `delete_prefix`) |
| Fast artifacts | `data_sources/fast/artifacts.py:40` | — |

### B. "Caches" — in k8s almost none of these can stay local

A pod's disk is per-replica and lost on restart, so the test is: *does another
pod, or the same pod after a restart, need this file?*

| Data | Location | Verdict | Why |
|---|---|---|---|
| Learned SSH host keys | `services/git_hardening.py:42` (`git_state/known_hosts.learned`) | **Shared — move to DB** | Trust-on-first-use state (`accept-new`). Per-pod or lost on restart means every new pod re-trusts whatever key it sees first, which is a MITM window. Security state, not a cache. |
| Connector file copies | `_file_tool_common.py:1214`, `file_reference_service.py:63` (`source_kind="connector"`) | **Shared — S3** | `File` rows point at them; another pod has the row but not the bytes. Same treatment as uploads. |
| PBIX / QVD / PBIRS Parquet | `pbix_client.py:38`, `qvd_client.py:35`, `powerbi_report_server_client.py:32,37` | **Shared — S3** | Conversion takes minutes. Without sharing, the indexing job's output on pod A is useless to a query on pod B, and every restart reconverts. Key by file hash + schema version; `materialize()` before DuckDB reads. |
| Extracted text / rendered pages | `_file_cache.py:25` (`filecache`) | S3 preferred, local OK | Pure perf, but extraction (OCR/page render) is expensive enough that per-pod misses hurt. |
| Git clones | `services/repo_cache.py:51` | **Local OK** | Git needs a working tree; re-clone on miss is cheap and already handled (TTL + eviction). |
| `materialize()` copies (new) | `storage.cache.dir` | **Local OK** | Copies of objects already in S3. |

Local dirs in k8s must be an `emptyDir` with `sizeLimit` (and matching
`ephemeral-storage` requests). An unbounded cache otherwise gets the pod
evicted. Enforce `storage.cache.max_bytes` with LRU eviction.

### C. Consumers that read local paths → call `materialize()`

Sandbox file staging (`code_execution.py`, `sandbox/runner.py`), previews
(`file_preview*.py`), file tools (`read_file`, `grep_files`,
`read_excel_as_csv`, `_artifact_images.py:54,90`), `csv_client.py`,
`_office_convert.py`. Landlock must allow the cache dir, and
`network_dir_client.py:92-167` must also refuse the cache dir (it currently
refuses `uploads/`).

### D. Serving routes → stream or redirect

`routes/file.py`, `branding.py`, `user_profile.py`, `report.py`, `artifact.py`,
`dashboard.py`, `embed.py` currently `FileResponse(path)`. Switch to
`StreamingResponse(storage.open_stream(key))`, or 302 to a presigned URL when
`presign_downloads: true`. **Keep auth checks in the app** — presigned URLs
must be short-lived and only minted after the existing access check
(`services/file_access_service.py`).

## Cross-cutting concerns

- **`File.path` semantics.** Store the storage key, not a filesystem path.
  Anything that prints it into LLM prompts or the sandbox should use the
  materialized path at run time. One Alembic data migration strips the
  `uploads/` prefix (or the backend tolerates both).
- **Paths baked into JSON.** `preview_images` lists, `thumbnail_path`, branding
  URLs and avatar URLs are stored as strings in rows — audit each so they stay
  key-shaped, not absolute paths.
- **Deletes.** Today some code deletes files with `os.remove`; every delete
  must go through `storage.delete`, or S3 leaks objects. Consider an S3
  lifecycle rule for `tmp/`.
- **Large files.** `upload_file` does `await file.read()` (whole file in
  memory). With S3 use multipart upload / `upload_fileobj` streaming.
- **Encryption.** `DataEncryptionConfig` (enterprise) covers DB columns only;
  S3 adds SSE-S3/SSE-KMS. Document that files aren't covered by app-level
  encryption either way.
- **Audit.** File reads/writes go through `ee/audit` in places; make sure the
  storage layer doesn't bypass them.
- **Concurrency locks.** PDF rendering uses per-artifact locks keyed on the
  file path (`routes/report.py:837`, `artifact.py:632`). Those are
  in-process; multi-replica needs a DB/Redis lock or "render to temp, then
  put" semantics.
- **Health check.** Add a startup probe that does `put`/`get`/`delete` on a
  canary key so bad credentials fail fast.

## Deployment changes

- `Dockerfile:235` — keep `mkdir uploads/...` for local mode; add cache dir.
- `docker-compose.yaml:75-76`, `deploy/airgap/docker-compose.yaml:35-36` —
  volumes stay for `local`; add an optional MinIO service profile for testing.
- `k8s/chart` — add `storage.*` values → render into `bow-config.yaml`; support
  IRSA/Workload Identity via `serviceAccount.annotations`; add an optional
  uploads PVC for `local` mode (fixes the data-loss gap above independently of
  S3).
- Docs: docs.bagofwords.com self-hosting page (via the `docs-update` skill).

## Migration for existing installs

`backend/scripts/migrate_uploads_to_s3.py`: walk `uploads/` (category A dirs
only), `put` each object with the same relative key, verify by size/etag,
then flip `storage.backend: s3`. Idempotent and resumable. Local files left in
place until the operator deletes them.

## Tests

- Run the existing file/upload/PDF/thumbnail tests against both backends via a
  fixture param; use `moto` (in-process S3) — no network.
- New unit tests for `S3Backend` (key validation, prefix, presign, delete_prefix,
  materialize cache hits/misses/eviction).
- Sandbox e2e: upload → S3 → coder reads `excel_files[0].path` (use the
  `sandbox-feedback-loop` skill with a MinIO container).
- 18 test files hardcode `uploads/` and will need the storage fixture.

## Suggested order

1. `StorageBackend` + `LocalBackend` + config, no behavior change.
2. Route all **writes** in group A through it.
3. Route all **serving** routes (group D) through it.
4. Add `materialize()` and move group C consumers.
5. Move SSH host keys to the DB; move connector copies and Parquet caches to
   storage; repoint git clones at `storage.cache.dir`.
6. `S3Backend` + moto tests + MinIO compose profile.
7. Migration script, Helm values, docs.

Steps 1–5 are pure refactors that ship safely on local disk; S3 becomes a
config flip at step 6.
