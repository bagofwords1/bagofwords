# Custom tables cached per person — design

A custom table on a connection where **each person signs in with their own
credentials** (`auth_policy='user_required'`) is cached once per person. Each
copy is extracted with that person's own credentials, so the source's own
row-level security decides what it holds: a Postgres policy, a Power BI RLS
role, a warehouse grant. BOW adds no row rule of its own and keeps no mapping
that could drift from the source's.

Status: built, behind the existing `enable_custom_queries` beta switch.
Live loops and measured limits: `docs/feedback-loops/custom-table-user-copies.md`.

## Why

Custom tables were refused on `user_required` connections. One shared copy
would hold the rows of whoever refreshed it and hand them to every reader.
That excluded exactly the customers who need acceleration most and trust
their source's row-level security most.

`custom-query-rls.md` solves the neighbouring problem — one shared copy,
filtered at read time by a policy the admin writes in BOW. That makes BOW the
authority for who sees what. Per-person copies keep the source as the
authority, at the price of one extraction and one file per person.

| | Shared copy + BOW policy (`custom-query-rls.md`) | Copy per person (this doc) |
|---|---|---|
| Connection | shared credentials | each person's own credentials |
| Who decides the rows | a policy the admin writes in BOW | the source, for that person's login |
| Extractions | one per refresh | one per person, when they need it |
| Storage | one file | one file per person |
| Can drift from the source's rules | yes (hence the conformance check) | no; at most stale by the maximum age |

## Model

`custom_table_user_copies` (migration `ucopy01`), one row per (table, user):

```
connection_table_id, connection_id, organization_id, user_id
status                      building | ok | error | evicted
artifact_path, artifact_key_enc, artifact_bytes, row_count, columns
definition_hash             the query (and Power BI target) it was built from
credential_fingerprint      which login it was read with (keyed digest)
failed_definition_hash, failed_credential_fingerprint    the last failed attempt
refreshed_at, build_ms, last_used_at, building_since
invalidated_at              do not serve until rebuilt
refresh_requested_at        serve, and rebuild behind
```

The table's definition, columns, description and activation stay on
`ConnectionTable`, unchanged. On a per-user connection that row carries **no**
artifact, no row count and no schedule.

## When a copy is served

`app/data_sources/fast/user_copies.py: evaluate()` — pure, no I/O.

1. **Never** when it was built from something else: another query
   (`definition_hash`), another login (`credential_fingerprint`), or it was
   invalidated, or it is older than the organization's maximum age.
2. **Served and rebuilt behind** when it is only past the table's own
   schedule, or a sign-in asked for a refresh. It is still that person's own
   rows and still inside the maximum age.
3. **Served as is** otherwise.

The maximum age (`custom_table_user_copy_max_age_hours`, default 24) is what
bounds how long a change to someone's access at the source can go unnoticed: a
copy is a snapshot of what they could see when it was built.

The credential fingerprint is an HMAC, keyed with the deployment secret, over
the resolved credentials with token material removed and the account the token
names (`tid`/`oid`/`upn`…) put in its place. Refreshing a token is the same
person; reconnecting as another account, or storing another database login,
is not.

## Building

`custom_table_user_copy_service.serve()` runs inside `construct_clients`, i.e.
before the agent's schema context is built and before any SQL is written.

- A missing or unservable copy is built **with the credentials that were just
  resolved for the live client** — the cached rows and the live rows are always
  the same login's.
- The question waits up to `custom_table_user_copy_wait_seconds` (default 30),
  counted from when the build began. A slower build carries on in the
  background and that question is answered from the live source: same person,
  same rows, only slower. The next question finds the copy.
- One builder per (table, user): an in-process task registry, and a file lock
  that also holds across workers and is released by the kernel if the process
  dies.
- At most `custom_table_user_copy_concurrency` (default 4) builds at once per
  connection, per worker.
- A failed build is recorded and not repeated for five minutes for the same
  query and login.
- Extraction is the existing one (`extractor.extract_to_artifact`): estimate,
  budget check, streamed batches, the organization's row and size limits.
  Always a full load — incremental refresh is refused on these connections,
  because there is no single checkpoint when every person holds different rows.

**Publishing is conditional.** The row is updated only if it is still the
build this run started and nothing invalidated it meanwhile, and only if the
table still exists. Otherwise the new file is discarded. A build that finishes
after a purge, a disconnect or a delete cannot bring the data back.

**A replaced file outlives the rebuild for an hour when it was being served.**
A chat that started before the rebuild holds that file's path for the rest of
its run. Deleting it at once took the relation away mid-answer (found in the
live loop). A file that was *not* servable — another login's rows, an older
query — is deleted the moment its replacement lands.

## Serving

`build_fast_client(rows, user_copies_by_table=…)`: on a per-user connection a
relation is attached only from the asking person's copy. A shared artifact
left on the row by a change of auth policy is never used. A table with no
copy is simply not in the DuckDB session.

The schema context shows a per-user table exactly when that person's copy is
servable (`servable()`), with the copy's own age. The prompt never names a
cached relation the session does not hold.

The DuckDB session is locked as before (`enable_external_access=false`,
`lock_configuration=true`), and each file has its own encryption key, so SQL
written against one person's session cannot attach another person's file.

## What "refresh" and the schedule mean here

- No job runs on a clock. Nobody is signed in at 3am, so a tick has no
  credentials to use.
- The table's interval or time of day says how old a person's copy may get
  before it is rebuilt at their next question.
- **Refresh now** (in the UI: *Rebuild everyone's copies*) re-checks the query
  and invalidates every copy; each is rebuilt at its owner's next question.
- Saving the table runs the query bounded to 100 rows, with the admin's own
  credentials, only to learn the columns.

## Removal

| Event | What happens |
|---|---|
| Admin deletes the table | every copy and file removed |
| Admin deletes copies in the dialog | files removed; rebuilt on next use |
| A person disconnects / removes their login | their copies on that connection removed |
| A person leaves the organization | copies invalidated in the same transaction; files removed by the hourly cleanup |
| Connection switched to shared credentials | copies removed; tables go back on their schedules |
| Connection switched to per-user | shared artifacts removed; schedules dropped |
| Not used for `custom_table_user_copy_idle_days` (7) | removed by the hourly cleanup |
| Older than the maximum age | removed by the hourly cleanup |
| Organization over `custom_table_user_copies_max_gb` (20) | least recently used removed first |

The daily orphan sweep counts copy files as referenced, so it never deletes
one that is being served.

## Settings

All under **Custom tables** in AI settings; ranges enforced in
`fast/limits.py`.

| Setting | Default | Range |
|---|---|---|
| `custom_table_user_copy_max_age_hours` | 24 | 1–720 |
| `custom_table_user_copy_idle_days` | 7 | 1–365 |
| `custom_table_user_copy_wait_seconds` | 30 | 1–600 |
| `custom_table_user_copies_max_gb` | 20 | 1–2,048 |
| `custom_table_user_copy_concurrency` | 4 | 1–32 |

The existing `custom_table_max_rows` / `_gb` / `_requests` apply to each
person's copy.

## API

- `CustomQuerySchema` gains `per_user` and `user_copies {copies, bytes}`.
- `GET  /connections/{id}/custom-queries/{cq}/user-copies` — who holds a copy.
- `DELETE /connections/{id}/custom-queries/{cq}/user-copies[?copy_id=]` — remove all, or one.
- Both need `manage_connection`, like every custom-table route.

## What this deliberately does not do

- **No scheduled or background refresh for people who are not there.** A
  stored login could be used at 3am, but that is a per-person extraction for
  everyone on every tick, mostly for people who will not ask anything that
  day. A copy is rebuilt when its owner needs it.
- **No sharing of identical copies.** Two people in the same RLS role get two
  files. Deduplicating by content would cut storage, not extraction, and
  would need a way to prove two logins see the same rows without reading both.
- **No measures.** A Power BI copy holds the rows a DAX query returned. To
  cache a measure correctly, the admin puts it in the query at the grain they
  want (`SUMMARIZECOLUMNS(...)`); see the loop doc for why raw fact tables are
  the wrong thing to copy there.
- **Not a cross-worker concurrency cap.** The per-connection limit is per
  worker process.
