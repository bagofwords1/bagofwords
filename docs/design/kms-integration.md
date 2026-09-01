# KMS integration — design

Status: design, not implemented. This document proposes how Bag of Words should
integrate with external key-management services (AWS KMS, GCP Cloud KMS, Azure
Key Vault, HashiCorp Vault) so that the root of trust for every secret we store
moves out of a plaintext environment variable and into a hardware-backed,
auditable, rotatable key service.

## Where we are today

Every secret in the product is protected by **one symmetric Fernet key**,
`settings.bow_config.encryption_key`, supplied as the raw `BOW_ENCRYPTION_KEY`
environment variable and frozen into a plain `str` at process start
(`backend/app/settings/config.py:139`). It is used *directly* — no envelope, no
data keys — by roughly ten model classes that each re-implement
`Fernet(settings.bow_config.encryption_key)` inline:

| Secret | Where encrypted | Column |
|---|---|---|
| Data-source credentials (incl. OAuth client secrets) | `app/models/connection.py:212-222` | `connections.credentials` (Text) |
| Per-user connection creds + OAuth access/refresh tokens | `app/models/user_connection_credentials.py:53-63` | `encrypted_credentials` (Text) |
| Per-user data-source creds (legacy sibling) | `app/models/user_data_source_credentials.py:38-46` | `encrypted_credentials` (Text) |
| LLM provider API key + secret | `app/models/llm_provider.py:95-102` | `api_key`, `api_secret` (Text) |
| Slack / Teams / WhatsApp / email platform creds | `app/models/external_platform.py:22-32` | `credentials` (Text) |
| Git SSH key and PAT | `app/models/git_repository.py:53-75` | `ssh_key`, `access_token` (Text) |
| Webhook signing secret | `app/models/webhook.py:90-96` | `secret_encrypted` (String) |
| Per-org SMTP password | `app/services/email/secrets.py:17-33` | inside `OrganizationSettings.config` JSON |
| Per-artifact DuckDB key (**already an envelope**) | `app/data_sources/fast/artifacts.py:38-52` | `connection_tables.artifact_key_enc` |
| Analytical payloads (enterprise `data_encryption`) | `app/ee/encryption/types.py` | `EncryptedJSON` columns on Step/Entity/ToolExecution/… |

Two additional facts shape everything below:

- **The key doubles as JWT signing material.** `app/core/auth.py:40` sets
  `SECRET = settings.bow_config.encryption_key` and uses it for session JWTs,
  password-reset and verification tokens. So the key is not merely a
  key-encryption key — it is HMAC material that must be *extractable*.
  (`app/core/file_tokens.py:26-33` already shows the better pattern: derive a
  purpose-specific secret with SHA-256 instead of reusing the key verbatim.)
- **Ciphertexts are opaque.** Every Text column holds a bare Fernet token with
  no key id, version, or provider tag. There is no way to tell which key wrote
  a ciphertext, which means no dual-key reads, and therefore no rotation. Today
  "rotation" is: change `BOW_ENCRYPTION_KEY` and permanently brick every stored
  credential — a failure mode the codebase warns about in at least eight places
  (`bow_config.py:316-318`, `k8s/README.md:153-158`, several feedback-loop docs).

There is no KMS, Vault, or cloud secret-manager usage anywhere in the backend
today (the `kms_key` in `aws_athena_client.py` is a passthrough for the
customer's Athena result encryption, not ours), and no key-rotation machinery.

## Why KMS, and what it must buy us

Enterprise buyers ask three things of our secret storage that an env var cannot
answer:

1. **Root of trust outside the app.** A compromised container image, config
   dump, or ConfigMap must not yield the key that opens every warehouse
   credential in the org. Helm currently emits `BOW_ENCRYPTION_KEY` into a
   **ConfigMap** when set as a literal (`k8s/chart/templates/config.yaml:43-47`).
2. **Rotation without data loss.** Rotate the key on a schedule or after an
   incident, with old data still readable and lazily re-encrypted.
3. **Auditability.** Every use of the root key shows up in CloudTrail / Cloud
   Audit Logs / Vault audit devices, tied to the workload identity that made it.

## Recommended architecture: KMS-wrapped data keys (envelope encryption)

Keep all row-level cryptography exactly as it is — local, synchronous Fernet —
and change **where the Fernet key comes from and how it is identified**.

```
                 ┌──────────────────────────────┐
                 │  KMS / Key Vault / Vault      │   KEK: never leaves the KMS.
                 │  KEK: bow-root (customer key) │   Used only to wrap/unwrap DEKs.
                 └──────────────┬───────────────┘
                                │ Decrypt(wrapped_dek)   ← one call at startup,
                                ▼                          one per rotation
   app DB: encryption_keys table
   ┌───────────────────────────────────────────────┐
   │ key_id │ provider │ kek_ref │ wrapped_dek │ state │
   │ k1     │ aws_kms  │ arn:…   │ AQICAH…     │ retired│
   │ k2     │ aws_kms  │ arn:…   │ AQICAH…     │ active │
   └───────────────────────────────────────────────┘
                                │ unwrapped in memory
                                ▼
   keyring: {k1: Fernet, k2: Fernet, legacy: Fernet(BOW_ENCRYPTION_KEY)}
                                │
        writes use `active`     │     reads pick by key id in the ciphertext
                                ▼
   ciphertext: bow1:k2:gAAAAB…          (legacy rows: bare Fernet token)
```

- A **KEK** (key-encryption key) lives in the customer's KMS and never leaves
  it. The app holds only IAM/workload-identity permission to call
  `Decrypt`/`Encrypt` (or Vault `transit/{encrypt,decrypt}`) on that one key.
- **DEKs** (data-encryption keys) are 32-byte Fernet keys generated by us (or
  by `GenerateDataKey`), stored **only in wrapped form** in a new
  `encryption_keys` table. Storing ciphertext in our own DB is safe — only the
  KMS can open it — and it gives rotation a natural home: a new DEK is a new
  row, not a config change.
- At startup the app unwraps the active DEK(s) once and holds them in memory.
  **No KMS call ever sits on a request path.** This matters doubly here because
  several decrypt sites are synchronous SQLAlchemy `TypeDecorator`
  bind/result processors (`app/ee/encryption/types.py`) that cannot await a
  network call.

This is the same envelope shape the codebase already uses twice: the
per-artifact DuckDB keys (`app/data_sources/fast/artifacts.py` — a random DEK
Fernet-wrapped by the root key) and, structurally, the `EncryptedJSON` JSON
envelope. We are generalizing an existing pattern, not importing a foreign one.

### Alternatives considered and rejected

- **Direct KMS encrypt/decrypt per secret (no local DEK).** Every credential
  read becomes a network call: latency and cost on the hottest paths, a hard
  dependency on KMS availability for every query, impossible inside the sync
  `TypeDecorator` read path, and dead on arrival for the airgap deployment
  (`deploy/airgap/`). Vault's transit engine has the same shape. Rejected as
  the default; a "non-extractable" mode can come later for customers who demand
  it (see Phase 4).
- **Only fetching `BOW_ENCRYPTION_KEY` from a cloud secret manager** (External
  Secrets Operator, `secretRef`, Vault agent injector). Better delivery, same
  trust model: the raw key still materializes in the pod env and in etcd, no
  rotation story, no audit of key *use*. Worth documenting as an operator
  quick-win today (it needs zero code), but it is not the feature.
- **Database-level encryption (pgcrypto / TDE / encrypted EBS).** Protects the
  disk, not the application boundary: anyone with the app's DB URL reads
  plaintext. Orthogonal; customers can and do layer it anyway.

### Ciphertext format and compatibility

New writes to the Text columns get a self-describing prefix:

```
bow1:<key_id>:<fernet token>
```

`bow1:` is unambiguous — a bare Fernet token always starts with `gAAAA` — so
reads dispatch on the prefix: prefixed → look up `key_id` in the keyring;
bare token → the legacy `BOW_ENCRYPTION_KEY`. For `EncryptedJSON`, the JSON
envelope grows an optional `"k": "<key_id>"` field (`_ENVELOPE_VERSION` is
written today but never checked on read — `types.py:247-257` — so adding a key
is backward-compatible by construction).

The compatibility contract mirrors the one `app/ee/encryption` already
established, because it is the right one:

- **Reads are never gated.** A lapsed license, a disabled provider, or a
  toggled-off feature must never turn stored credentials into ciphertext soup.
  Legacy bare-token rows stay readable forever as long as `BOW_ENCRYPTION_KEY`
  remains configured; there is no forced backfill.
- **Writes are gated** on the provider being configured and healthy. Rows
  migrate lazily as they are rewritten, plus an explicit re-encryption job for
  operators who want to finish the migration (Phase 3).

### The provider abstraction

Copy `app/settings/db_auth.py` — the `DatabaseAuthProvider` Protocol with a
name→factory registry and config-driven selection — whose own docstring already
anticipates this ("Future: 'azure_entra', 'gcp_iam'"). Sketch, not final code:

```python
class KeyProvider(Protocol):
    def wrap(self, plaintext_dek: bytes) -> WrappedKey: ...
    def unwrap(self, wrapped: WrappedKey) -> bytes: ...
    def describe(self) -> KeyProviderInfo: ...   # provider name, kek ref, health

_PROVIDERS = {
    "local":           ...,  # today's behavior: BOW_ENCRYPTION_KEY is the DEK
    "aws_kms":         ...,  # boto3 kms Encrypt/Decrypt; auth via IRSA/env/instance role
    "gcp_kms":         ...,  # google-cloud-kms; auth via Workload Identity
    "azure_key_vault": ...,  # azure-keyvault-keys wrap/unwrap; azure-identity (already a dep)
    "vault":           ...,  # Vault transit encrypt/decrypt via hvac
}
```

Config, following the existing `DatabaseAuth` shape in `bow_config.py`:

```yaml
encryption:
  provider: aws_kms            # local (default) | aws_kms | gcp_kms | azure_key_vault | vault
  key_reference: arn:aws:kms:us-east-1:123456789:key/abc-…   # KEK id/ARN/URI/path
  region: us-east-1            # provider-specific extras as needed
  # vault only:
  # address: https://vault.internal:8200
  # transit_key: bow-root
  # auth: kubernetes | token

encryption_key: ${BOW_ENCRYPTION_KEY}   # unchanged; legacy DEK + reader of old rows
```

Provider auth deliberately leans on ambient workload identity — IRSA, GCP
Workload Identity, Azure managed identity, Vault Kubernetes auth — never
long-lived cloud keys in config. The Helm chart already annotates the
ServiceAccount for exactly this (`k8s/chart/values.yaml:74-76`,
`templates/sa.yaml`).

`boto3` and `azure-identity` are existing dependencies; `google-cloud-kms` and
`hvac` would be new (small, optional extras — consider packaging as
`bagofwords[kms-gcp]`-style extras so the airgap image stays lean).

### Bootstrap, startup, and failure semantics

- **Bootstrap** (first boot with a KMS provider configured, empty
  `encryption_keys` table): generate a 32-byte DEK locally, `wrap()` it, insert
  the row as `active`, proceed. Idempotent under multi-worker start — insert
  with a unique partial index on `state='active'` and lose-and-reread on
  conflict (the multi-worker key bug documented in
  `docs/feedback-loops/mcp-approval-multiworker.md` is the cautionary tale).
- **Startup**: unwrap all non-retired DEKs, build the keyring, cache in the
  process. KMS unreachable → **fail startup loudly**. A half-alive instance
  that can serve traffic but cannot open any credential produces far more
  confusing failures than a crash-looping pod with a clear error, and readiness
  probes make this operable.
- **Runtime**: no KMS dependency. An outage after boot degrades nothing.
- **The ephemeral-key latch** (`_encryption_key_is_ephemeral`,
  `bow_config.py:243-252`) generalizes into the keyring's provenance: the EE
  payload-encryption write gate (`encryption_active()`) changes its question
  from "is the key ephemeral?" to "is the active DEK durable?" — true for any
  KMS-managed DEK, unchanged semantics for `local`.

### JWT and derived secrets

`core/auth.py` must stop using the KEK-protected DEK verbatim. Adopt the
`file_tokens.py` pattern: `sha256(b"bow-jwt:" + active_dek)` (or HKDF with
purpose labels) for session/reset/verification secrets. Consequence to accept
and document: rotating the DEK invalidates outstanding sessions and reset
tokens — with 7-day session lifetimes this is a reasonable, even desirable,
property of a rotation. If product disagrees, a separately-rotated `signing`
purpose key in the same `encryption_keys` table solves it; that is a decision
for implementation time, not an architectural fork.

### Rotation

- **KEK rotation** is the customer's KMS's job (AWS rotates backing material
  in place; Vault transit versions keys). Wrapped DEKs keep decrypting across
  KEK versions; optionally re-`wrap()` on a schedule. Zero data touched.
- **DEK rotation** (admin-initiated, Settings → Security or CLI): generate and
  wrap a new DEK, insert as `active`, demote the old to `read-only`. New writes
  carry the new key id; old rows remain readable via the keyring.
- **Re-encryption job** (optional completion): a background walker that reads
  and rewrites rows carrying retired key ids — the `bow1:` prefix and the
  `envelope_marker_sql()` trick (`types.py:308-321`) make "which rows still use
  k1" a cheap SQL question for Text and JSON columns respectively. When no
  ciphertext references a DEK, it can be marked `retired` and its unwrap
  skipped at boot.
- This also finally gives **legacy → managed migration** a path: the same job
  rewrites bare-token rows under the active DEK, after which
  `BOW_ENCRYPTION_KEY` can be deleted from the environment entirely.

### Licensing and packaging

Gate *configuring a non-`local` provider* as an enterprise feature (add `kms`
to `TIER_FEATURES["enterprise"]` in `app/ee/license.py`, provider factory
checks `has_feature("kms")`), with provider implementations living under
`app/ee/kms/`. Decryption is never license-gated, per the compatibility
contract. The `local` provider — today's behavior — remains the community
default and the airgap answer.

## Phasing

**Phase 0 — consolidate (no KMS, no behavior change).** Collapse the ~10
inline `Fernet(...)` implementations into one `app/core/crypto.py` (the 33-line
`app/services/email/secrets.py` is the shape to grow); route
`app/ee/encryption/types.py` and `fast/artifacts.py` through it; introduce the
keyring + `bow1:` prefix machinery with a single `local` key; fix the JWT
secret to a derived value; add the missing round-trip and wrong-key tests
(today nothing tests `Connection.encrypt_credentials` at all). This phase is
pure refactor and is what makes every later phase small.

**Phase 1 — first provider.** `KeyProvider` protocol + config model +
`encryption_keys` table/migration + bootstrap/startup semantics + `aws_kms`
(boto3 is already a dependency, IRSA plumbing already in the chart). Helm:
`config.encryption.*` values, README, NOTES warning parity with the existing
ephemeral-key warning. Docs: a real key-management page under `documents/`.

**Phase 2 — provider breadth.** `gcp_kms`, `azure_key_vault`, `vault`
(transit). Each is ~a hundred lines against the protocol plus auth wiring and
docs.

**Phase 3 — rotation UX.** Admin rotation endpoint/UI, the re-encryption
walker, key-inventory visibility (which key ids still hold data), legacy-key
retirement flow.

**Phase 4 — optional hardening, if customer demand exists.** Non-extractable
mode (per-request Vault transit / KMS Decrypt with short-TTL DEK caching) for
customers whose policy forbids DEKs in app memory; per-org DEKs (BYOK) for
multi-tenant isolation — the `encryption_keys` table is already shaped for
both (add an `organization_id` column), but neither should block shipping
Phases 0–3.

## Test plan

- Unit: round-trip per secret surface through `app/core/crypto.py`; prefix
  dispatch (bare token → legacy key, `bow1:k2:` → keyring); wrong-key and
  missing-key behavior (credentials: raise; EE payloads: log + `None`, per the
  existing contract in `test_encrypted_payloads.py`); JWT derivation.
- Provider: `aws_kms` against moto; `vault` against a dev-mode Vault container;
  fake-provider tests for bootstrap idempotence under concurrent workers and
  fail-hard on unwrap failure.
- Rotation: write under k1, rotate to k2, read both, re-encrypt, retire k1,
  verify boot without k1.
- E2E: existing connection/LLM/OAuth suites run unchanged under
  `provider: local` (proving Phase 0 changed nothing), plus one e2e with a
  fake KMS provider enabled.

## Open questions

1. **Session invalidation on DEK rotation** — accept (recommended) or add a
   separately-rotated signing key? Decide in Phase 0 when `auth.py` is touched.
2. **Wrapped-DEK storage: DB table vs env blob.** This design says DB
   (rotation needs multiple keys and mutability; env cannot be written back).
   Confirm nobody needs the stateless-env variant before the migration lands.
3. **Fail-hard on KMS outage at boot** — any appetite for a read-only degraded
   mode instead? Recommendation is no (complexity outruns the benefit).
4. **Which provider second** — GCP vs Azure vs Vault should be ordered by
   actual customer asks, not alphabet.
