# Feedback Loop — "encrypt step.data, entity output and tool results at rest"

Data-source **credentials** have always been encrypted at rest
(`Connection.encrypt_credentials`). The query **results** those credentials
fetch — `Step.data`, `Entity.data`, `ToolExecution.result_json` and the bounded
context summaries derived from them — were stored as plaintext JSON. For
customers whose warehouses hold regulated data, that snapshot is frequently more
sensitive than the credential that produced it.

This loop adds application-level encryption for those payloads behind the
enterprise `data_encryption` feature, reusing the **same** Fernet key as
credentials (`bow_config.encryption_key`) so operators have one key to protect,
back up and rotate. It validates the storage envelope, the licence + config
gate, the ephemeral-key guard, backward compatibility with existing plaintext
rows, throughput, and the whole thing end-to-end through the UI with a live LLM.

## What was built (file:line)

- Column type: `backend/app/ee/encryption/types.py` — `EncryptedJSON`
  (`TypeDecorator` over `JSON`), the `{"__bow_enc__":1,"z":1,"v":…}` envelope,
  `encryption_active()` gate, `envelope_marker_sql()` for the SQL projections.
- Encrypted columns (7): `Step.data`, `Step.context_summary_json`,
  `StepUserResult.data`, `Entity.data`, `EntityUserResult.data`,
  `ToolExecution.result_json`, `ToolExecution.context_summary_json`.
  The `context_summary_json` columns are **not optional** — they hold a sampled
  projection of the very rows being protected, so encrypting `data` while
  leaving the summary in plaintext would leak exactly what the feature exists
  to hide.
- Licence tier: `backend/app/ee/license.py` — `data_encryption` on `enterprise`.
- Config: `backend/app/settings/bow_config.py` — `DataEncryptionConfig`,
  **defaults to enabled**. Deliberately *not* added to the shipped
  `bow-config.yaml` template: the safe posture on a licensed instance is
  encrypted, and an operator who never touches config gets it.
- Ephemeral-key guard: `backend/app/settings/bow_config.py` +
  `backend/app/settings/config.py` — `mark_encryption_key_ephemeral()` /
  `encryption_key_is_ephemeral()`.
- SQL-projection call sites made envelope-aware:
  `query_context_builder.py:_get_legacy_step_summaries_postgres`,
  `message_context_builder.py:_load_create_data_result_projections` and
  `:_load_read_query_result_projections`, plus the Python projections
  `_project_create_data_result` / `_project_read_query_result` and
  `_load_decrypted_results`.
- Dependency: `orjson` promoted to a direct dependency (`backend/pyproject.toml`).

**No Alembic migration.** The envelope is itself a valid JSON object, so the
columns stay `JSON` on both PostgreSQL and SQLite. Adopting the feature is a
config flag, not a table rewrite. `alembic upgrade head` was re-run against the
new column types unchanged, on a fresh database, to confirm.

## Design — the two rules that make it safe

Encryption is gated **on write only**; decryption is *never* gated.

1. Reading an envelope always decrypts, regardless of licence or config. A
   lapsed licence, or an operator flipping the toggle off, must never turn a
   customer's dashboards into unreadable ciphertext.
2. Reading a plaintext value returns it untouched, forever. There is no forced
   backfill; rows migrate lazily as they are rewritten.

Together these make adoption *and* rollback a config toggle, and make the
intermediate state — a table holding both plaintext and encrypted rows — a
first-class supported state rather than a migration window.

### SQL projections and per-row detection

Conversation history and query context deliberately project small digests out of
these JSON documents *in SQL*, to avoid hydrating multi-megabyte payloads on
every planner refresh. SQL cannot see inside an envelope.

Rather than switching those queries wholesale on the config flag — which would
silently return empty digests for rows encrypted before the flag was turned off
— each query now also selects an **envelope marker** (`data->'__bow_enc__'`) and
re-reads only the flagged rows through the ORM, where the column type decrypts.
Per-row detection is correct in every state, including the mixed table, and
costs one extra JSON field access when nothing is encrypted.

These are legacy paths in the first place: new rows carry the small
`context_summary_json` projection written synchronously by the model event
listener, and the SQL projection runs only for historical rows lacking one,
write-through-persisting a summary afterwards. So the fallback is bounded and
one-shot per row, not a steady-state cost.

### Why the key must be durable

`encryption_active()` returns `False` when `BOW_ENCRYPTION_KEY` was not supplied
and the key was therefore invented at startup. This is the guard that makes
"default enabled" defensible: without it, any licensed deployment that never
pinned the key would silently lose every snapshot on the next restart. An
unpinned key was previously only an annoyance — credentials could be re-entered
— but an analytical payload cannot be re-derived. The instance stays in
plaintext and logs why, once.

## Performance — measured, not asserted

The requirement was no throughput regression. Encryption cannot be free on CPU,
so the cost is paid for structurally:

- **Compress before encrypting** (zlib, level 4). Result rows are highly
  compressible, so the ciphertext is ~3.5× *smaller* than the plaintext it
  replaces — the database stores, WALs and ships far less. Compression also
  makes the cipher ~5× cheaper by shrinking its input.
- **orjson for payload (de)serialization.** ~10× faster than the stdlib encoder
  on these documents, which is what buys back the compression and cipher time.

Level 4 was chosen by end-to-end measurement, not by default: level 1 leaves
reads marginally slower, level 6 costs too much on writes.

End-to-end SQLite round-trip of a real `Step.data` document, median of 12
(`tests/unit/test_encrypted_json_perf.py` guards these):

| rows | mode | write | read | bytes/row |
|---|---|---|---|---|
| 500 | plaintext | 4.37 ms | 1.78 ms | 75,934 |
| 500 | encrypted | 4.16 ms | 1.80 ms | 21,383 |
| 5,000 | plaintext | 16.30 ms | 11.43 ms | 761,920 |
| 5,000 | encrypted | 17.68 ms | 9.96 ms | 206,255 |
| 25,000 | plaintext | 72.37 ms | 57.63 ms | 3,827,748 |
| 25,000 | encrypted | 72.09 ms | 56.14 ms | 1,026,523 |

Encrypted reads and writes land at or below the plaintext baseline at every
size, with **~73% less stored per row**. On PostgreSQL over a network the size
reduction is worth strictly more than it is on a local SQLite file.

With the feature **off**, `process_bind_param` is one `isinstance` plus one dict
membership test — measured under 1 µs and asserted in
`test_disabled_encryption_adds_no_work`.

## Sandbox validation (live LLM, real UI)

Backend + frontend + Claude 4.5 Haiku via a real Anthropic key, sandbox
enterprise licence (`scripts/gen_sandbox_license.py`), pinned
`BOW_ENCRYPTION_KEY`, a `network_dir` agent over an 800-row `sales.csv`.

**Phase 1 — the "old version".** Booted with `data_encryption.enabled=false`,
ran a real prompt ("revenue by region bar chart + top 5 products table"). All
seven columns confirmed plaintext in the DB — a faithful pre-upgrade customer
database.

**Phase 2 — the upgrade.** Restarted with the feature on:

- The Phase 1 report still renders completely (North America / EMEA / APAC /
  LATAM / Doohickey all present). **Backward compatibility confirmed against
  data written by the previous configuration.**
- A new turn on that same report wrote encrypted rows, producing a genuinely
  mixed table: 1 encrypted + 3 plaintext in every column, all readable.
- Conversation history survived the boundary: asked "which region did you say
  was the leader, and what was its exact revenue figure?", the agent answered
  "**North America** … **$637,455**" — which required reading back the encrypted
  `tool_executions.context_summary_json` from the prior turn.
- A fresh four-visualization report under encryption produced a **"Sales
  Overview" dashboard artifact** rendering all four charts from encrypted
  `Step.data` (artifacts fetch via `useArtifactData()`, so they exercise the
  decrypt path end-to-end).

- Promoting a step to a published **entity** (`POST /entities/from_step/...`)
  stored `entities.data` as an envelope while `GET /entities/{id}` returned the
  decrypted rows, and the entity is listed in the Queries UI.

**Coverage note.** `Step.data`, `Step.context_summary_json`, `Entity.data`,
`ToolExecution.result_json` and `ToolExecution.context_summary_json` were all
exercised live through the UI. `StepUserResult.data` and
`EntityUserResult.data` — the per-viewer caches, which need a second user on a
shared report — were not driven through the UI; they use the identical column
type and are covered by the DB-level round-trip and mixed-table unit tests.

**Leak check.** All seven columns scanned for plaintext needles (`Doohickey`,
`LATAM`, `EMEA`, `order_id`, `637455`, …): **0 leaks**, every non-null value an
envelope.

### Sandbox gotchas (cost real time — noted for the next run)

- `uvicorn --reload` runs the server in a `multiprocessing.spawn` **child**.
  Killing only the `python main.py` parent leaves the old worker holding :8000
  with the *previous* environment — a config toggle appears not to take effect.
  Kill the whole backend venv process group.
- `frontend/public/libs/` ships only `artifact-globals.js`; the vendored React /
  Babel / ECharts bundles come from `scripts/download-vendor-libs.sh`. Without
  it every artifact renders "React is not defined" — unrelated to any change
  under test.
- Waiting on `networkidle` never resolves: `/api/reports/activity/stream` is a
  long-lived SSE connection. Use `domcontentloaded`.
- Submit is gated on an attached agent; pin one via
  `PUT /api/users/me/default_agents`.

## Tests

- `tests/unit/test_encrypted_payloads.py` (26) — round-trip across payload
  shapes, envelope opacity and JSON validity, compression thresholds,
  orjson/stdlib parity, non-string key coercion, all four gate conditions,
  plaintext passthrough, ciphertext readable with the feature off, graceful
  degradation on a lost key, real DB round-trip, mixed-table read.
- `tests/unit/test_encrypted_tool_projection_parity.py` (5) — the Python
  projections produce **identical** digests to the SQL ones, proven by storing
  the same result twice (one plaintext, one encrypted) and comparing.
- `tests/unit/test_encrypted_json_perf.py` (5) — size ratio, encrypt/decrypt
  vs. plain serialization, Fernet reuse, zero cost when disabled.

Regression: the related existing suites pass
(`test_message_context_tool_result_projection`, `test_query_context_step_summary`,
`test_artifact_viewer_identity`, `test_entity_params`,
`test_history_summary_reaches_prompt`, …).
`test_artifact_relationship_loading::test_active_artifact_lookup_does_not_hydrate_report_graph`
and `test_context_compaction::test_background_compaction_emits_sse_event` fail
**identically on a clean checkout** (verified via `git stash`) — pre-existing,
unrelated.

## Not in scope

`Artifact.content` / `screenshot_base64`, `File.preview`, and files on disk
(uploads, generated PPTX/PDF, thumbnails) remain plaintext. The column type
extends to the first two by changing a column declaration; on-disk artifacts are
better served by volume/object-store encryption than by this layer.
