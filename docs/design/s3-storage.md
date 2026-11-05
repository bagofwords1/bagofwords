# Object storage backend (S3-compatible + Azure Blob)

Status: **implemented** (`storage.backend: local | s3 | azure`, default
`local`). The sections after "As built" are the original design map, kept for
context; where they differ, "As built" wins.

## As built

**One design choice drives everything:** the app keeps reading and writing real
files under `backend/uploads/` (the sandbox, pandas, Playwright and LibreOffice
need paths), and the object store is a mirror that is the source of truth.
`app/services/storage/` exposes:

| Call | When | Local backend |
|---|---|---|
| `persist(path)` / `apersist` | after writing a durable file | no-op |
| `ensure_local(path)` / `aensure_local` | before reading one (downloads on a miss; by default also checks the remote ETag, so a copy replaced or deleted on another pod is never served; `fresh=False` for write-once names) | `os.path.isfile` |
| `discard(path)` / `adiscard` | when deleting one | no-op |
| `persist_tree` / `ensure_local_tree` / `discard_tree` | slide previews, PBIX cache dirs | no-op / exists |
| `check()` | app startup: put/get/delete a canary, the pod exits if it fails | describes local |

Keys are the path relative to `uploads/` (`files/<uuid>_name.csv`), so no DB
value changed and no migration of rows is needed.

**Mirrored areas:** `files` (uploads, tool outputs, connector copies),
`thumbnails`, `pptx`, `pptx_previews`, `branding`, `avatars`, `demo_data`,
`fast`, `git_state` (learned SSH host keys, merged across pods),
`pbix_file_cache`, `pbix_data_cache`, `pbirs_schema_cache`, `qvd_cache`.

**Per-pod only** (`keys.LOCAL_ONLY_AREAS`): `repo_cache` (git mirrors),
`pdfs` (rendered and served in the same request), `artifact_js`
(content-addressed compile cache), `filecache` (fail-open read cache).

**Config:** `storage:` in bow-config, or `BOW_STORAGE_*` env (YAML wins).
Auth: S3 `default` (IRSA / Pod Identity / instance role) · `static` ·
`assume_role` (auto-refreshing STS); Azure `default` (Workload / Managed
Identity) · `service_principal` · `connection_string` · `sas`.

**Not built (follow-ups):**
- Signed-URL downloads (files stream through the app, keeping auth + audit).
- Remote GC of orphaned fast-mode extracts (`fast_artifact_maintenance`
  prunes only the local copy; orphans only come from a crash mid-refresh).
- The fast-mode refresh lock is a file lock per pod; two pods can refresh one
  connection at the same time (duplicate work, not corruption).
- Native GCS (GCS works through its S3-interoperable API with HMAC keys).

Verification: `docs/feedback-loops/object-storage-backends.md`.

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

| File | Role |
|---|---|
| `base.py` | `StorageBackend` protocol (below) + key validation |
| `local.py` | `LocalBackend` — today's behavior, root `uploads/`. Default. |
| `s3.py` | `S3Backend` — `boto3` (already a dep, `pyproject.toml:51`). AWS **and any S3-compatible** store via `endpoint_url`: MinIO, Cloudflare R2, Wasabi, Backblaze B2, Ceph RGW, NetApp StorageGRID, GCS (HMAC interop). |
| `azure_blob.py` | `AzureBlobBackend` — `azure-storage-blob` (**only new dep**; `azure-identity` is already a dep for the Kusto client). Azure Blob is not S3-compatible. |
| `_cache.py` | node-local LRU cache behind `materialize()` |
| `__init__.py` | `get_storage()` — picks the backend from config once at startup |

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
    async def signed_url(self, key: str, expires_s: int) -> str | None  # None for local
```

- Sync SDK calls wrapped in `asyncio.to_thread`.
- Keys are the existing relative paths minus the `uploads/` prefix
  (`files/<uuid>_<name>`, `thumbnails/<id>.png`, …) so `File.path` barely
  changes.
- Path-traversal guards (`safe_join`, `realpath` checks in routes) become key
  validation in one place.
- Each cloud backend is ~150–250 lines; the real cost is migrating the call
  sites (below), which is identical regardless of provider. After that, a new
  provider is one file + its contract tests.

**Rejected alternative:** `obstore` (Rust `object_store`) gives one async API
for S3/Azure/GCS with workload identity built in, but adds a native wheel and a
second AWS credential path next to boto3. Native SDKs reuse auth code we
already ship and are easier to support air-gapped.

### Provider differences (handled inside each backend)

| | S3 | Azure Blob |
|---|---|---|
| Signed download | presigned URL | user-delegation SAS (Entra-signed, no account key) |
| `delete_prefix` | list + `delete_objects` (1000/batch) | list + per-blob delete |
| Large upload | `upload_fileobj` multipart | `upload_blob` staged blocks |
| Server-side encryption | SSE-S3 / SSE-KMS (AWS only) | always on; CMK configured on the account |
| Compat gaps | some S3-compatibles lack checksums/tagging/SSE-KMS — gate per feature, never assume | — |

## Config

New `storage` section in `settings/bow_config.py` + `bow-config.yaml`:

```yaml
storage:
  backend: local              # local | s3 | azure
  prefix: "bow/"              # optional key prefix inside the bucket/container
  signed_downloads: false     # redirect downloads to short-lived signed URLs
  local:
    root: uploads
  s3:
    bucket: ""
    region: ""
    endpoint_url: ""          # MinIO / R2 / Wasabi / ...; empty = AWS
    addressing_style: auto    # auto | path | virtual (MinIO usually needs path)
    auth: default             # default | static | assume_role
    access_key_id: ""
    secret_access_key: ""
    session_token: ""
    role_arn: ""
    external_id: ""
    sse: ""                   # "" | AES256 | aws:kms
    kms_key_id: ""
  azure:
    account_url: ""           # https://<account>.blob.core.windows.net
    container: ""
    auth: default             # default | service_principal | connection_string | sas
    tenant_id: ""
    client_id: ""
    client_secret: ""
    connection_string: ""
    sas_token: ""
  cache:
    dir: uploads/.cache       # node-local; emptyDir in k8s
    max_bytes: 5GB
```

Every field has a `BOW_STORAGE_*` env override (matching `BOW_DATABASE_*`);
secrets are expected from env / k8s Secrets, not the YAML.

### Auth options

| Backend | `auth` | Mechanism | Use for |
|---|---|---|---|
| s3 | `default` | boto3 default chain: IRSA / EKS Pod Identity, ECS task role, EC2 instance profile, env vars, `~/.aws` profile | **Recommended on AWS — no stored secrets** |
| s3 | `static` | access key + secret (+ session token) | MinIO, R2, Wasabi, on-prem |
| s3 | `assume_role` | STS assume-role (optional `external_id`) on top of default or static creds | cross-account buckets |
| azure | `default` | `DefaultAzureCredential`: AKS Workload Identity, Managed Identity, env, Azure CLI | **Recommended on Azure — no stored secrets** |
| azure | `service_principal` | tenant + client id + secret (or cert) | non-AKS Azure, CI |
| azure | `connection_string` | account key in connection string | dev, Azurite |
| azure | `sas` | container-scoped SAS token | locked-down tenants |

Reuse, don't re-implement: lift the credential code in
`data_sources/clients/s3_client.py:21,247` (static / assume-role / default
chain) into a shared helper used by both the S3 data source and `S3Backend`.
This mirrors `DatabaseAuth.provider` (`settings/bow_config.py:299`), which
already plans `aws_iam` / `azure_entra`.

**Minimum permissions** (scoped to the bucket/container + prefix):
- S3: `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`;
  plus `kms:Encrypt`/`kms:Decrypt`/`kms:GenerateDataKey` if SSE-KMS.
- Azure: `Storage Blob Data Contributor` on the container; user-delegation SAS
  also needs `Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action`
  (included in Contributor at account scope, or grant
  `Storage Blob Delegator`).

**Startup health check:** `put`/`get`/`delete` a canary key under the prefix;
fail fast with a clear message naming the backend, bucket and the missing
permission.

## Inventory — what to migrate

### A. Durable user/app data → must go to object storage

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
| Git clones | `services/repo_cache.py:51` | **Local OK** | Read-only mirror of the remote (only `git_repo_client.py` uses it); writes in `git_service.py` clone→commit→push in a throwaway tempdir. Git needs a POSIX tree and the cache uses `fcntl` locks (`repo_cache.py:158`), so S3/NFS don't fit. Costs: per-pod cold clone (up to 300s, up to 5GB), pods may serve different commits within the 600s TTL (fix: pin the resolved SHA in the DB), and depends on shared SSH host keys above. |
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
  volumes stay for `local`; add optional `minio` and `azurite` profiles for
  testing.
- `k8s/chart`:
  - `storage.*` values → rendered into `bow-config.yaml`; secrets via
    `storage.existingSecret` → `BOW_STORAGE_*` env.
  - AWS: `serviceAccount.annotations` (`eks.amazonaws.com/role-arn`) for IRSA;
    nothing extra for Pod Identity.
  - Azure: `podLabels` (`azure.workload.identity/use: "true"`) +
    `serviceAccount.annotations` (`azure.workload.identity/client-id`).
  - `emptyDir` with `sizeLimit` for `storage.cache.dir` and git clones, plus
    `ephemeral-storage` requests.
  - Optional uploads PVC for `local` mode (fixes the data-loss gap above
    independently of object storage).
- Docs: docs.bagofwords.com self-hosting page, one section per provider with
  the IAM policy / role assignment (via the `docs-update` skill).

## Migration for existing installs

`backend/scripts/migrate_uploads_to_object_storage.py`: walk `uploads/` (category A dirs
only), `put` each object with the same relative key, verify by size/etag,
then flip `storage.backend` to `s3` or `azure`. Works for both via the same
`StorageBackend` interface. Idempotent and resumable. Local files left in
place until the operator deletes them.

## Tests

- **Contract suite**: one parametrized test module run against every backend —
  `local`, `s3` (`moto`, in-process, no network), `azure` (Azurite container in
  CI). Covers put/get/stream/exists/delete/delete_prefix/copy/signed_url, key
  validation, prefix handling, `materialize()` cache hit/miss/eviction.
- Existing file/upload/PDF/thumbnail tests run against `local` and `s3`
  (moto) via a storage fixture. 18 test files hardcode `uploads/` and need it.
- Auth: unit tests per `auth` mode with mocked credential providers (no real
  cloud in CI).
- Sandbox e2e: upload → bucket → coder reads `excel_files[0].path`, on MinIO
  and Azurite (`sandbox-feedback-loop` skill).
- Manual pre-release check on real AWS S3 (IRSA) and real Azure Blob
  (Workload Identity) — see "Verification" below.

## Suggested order

1. `StorageBackend` + `LocalBackend` + config, no behavior change.
2. Route all **writes** in group A through it.
3. Route all **serving** routes (group D) through it.
4. Add `materialize()` and move group C consumers.
5. Move SSH host keys to the DB; move connector copies and Parquet caches to
   storage; repoint git clones at `storage.cache.dir`.
6. `S3Backend` + shared AWS credential helper + moto contract tests + MinIO
   profile + Helm IRSA wiring.
7. `AzureBlobBackend` + Azurite contract tests + Helm Workload Identity wiring.
8. Migration script, real-cloud verification, docs.

Steps 1–5 are pure refactors that ship safely on local disk. Each cloud is then
one backend file + tests + Helm values.

## Verification (needs from the team)

CI covers everything with emulators (moto, MinIO, Azurite). Real-cloud checks
before release need:

**AWS**
- A test bucket (versioning off, no public access) in a sandbox account, with
  a dedicated prefix.
- An IAM role with the minimum policy above, trusted by: (a) an EKS cluster's
  OIDC provider for IRSA, and (b) optionally a second account for the
  `assume_role` test.
- A KMS key if SSE-KMS must be verified.
- For `static` mode: a short-lived access key for a user limited to that
  bucket.

**Azure**
- A storage account + container in a sandbox subscription.
- A user-assigned managed identity with `Storage Blob Data Contributor` (+
  `Storage Blob Delegator` for SAS downloads), federated to an AKS cluster for
  Workload Identity.
- For `service_principal` mode: an app registration with a client secret,
  same role.

**Either**
- A k8s cluster (EKS / AKS) where the Helm chart can be installed with 2
  replicas — the multi-pod case is the main thing emulators can't prove.
- Credentials delivered via the environment's secret store, never pasted into
  chat or committed.
