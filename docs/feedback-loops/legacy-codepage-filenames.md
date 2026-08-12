# Feedback Loop — "A legacy-codepage filename ends the whole agent turn"

Reproduces and validates the reported bug: reading a file off a share whose
filenames are stored in a legacy codepage (cp862 / DOS-Hebrew here) fails the
`files` INSERT and then kills every later statement in the same turn:

```
invalid input for query argument $1: '\udc84\udc8b\udc90\udc91\udc85\udc9a …'
('utf-8' codec can't encode characters in position 0-5: surrogates not allowed)
… This Session's transaction has been rolled back due to a previous exception
during flush.
```

The claim being validated: the filename reaching the `File` row still carried
surrogateescape'd bytes, and nothing rolled the session back afterwards.

---

## Root cause (validated)

Two defects chained.

**1. The name escaped the connector un-recovered.** `NetworkDirClient` recovers
legacy names for *listings* — `_entry` and `_rel_id` both go through
`recover_filename` (`network_dir_client.py:278,286`) — but returned `path.name`
RAW from `read_file` (`network_dir_client.py:415`) and `read_raw_bytes`
(`network_dir_client.py:545`), i.e. the surrogateescape'd directory-entry bytes.
That name rides `NamedBytes.name` into `_persist_session_file`
(`read_file.py:910`) and becomes the File row's `filename` **and** its on-disk
`path`. `files` is a UTF-8 table, so the driver rejects it outright.

The traceback in the report shows the split exactly: `source_ref` was clean
Hebrew (it came from the listing) while `filename` was mangled.

**2. Nobody rolled back.** The driver fails during *flush*, which leaves the
`AsyncSession` unusable. `attach_drive_file_to_session` treats persistence as
best-effort and swallowed the exception (`_file_tool_common.py:825` on main), so
the turn continued against a dead session — one un-storable filename ended the
run rather than costing just its own INSERT.

## The fix

1. Recover the name where it leaves the connector, so the id the model was given
   and the name it gets back agree (`network_dir_client.py`).
2. `storage_safe_name` at every persistence boundary — session attach, durable
   attach, user upload, inbound email attachment. It recovers the legacy
   encoding when one explains the bytes (a real Hebrew name beats `??????`) and
   hard-scrubs whatever is left. Applied **before** the storage path is derived,
   so disk and DB name the same file.
3. Roll back on a failed attach, in both attach paths, so a write that fails
   anyway costs the file rather than the turn.

---

## Sandbox reproduction

Fixture built with **bytes** paths, so the on-disk names really are non-UTF-8,
exactly as a Windows/DOS-era share leaves them:

```
readme.txt                                                      (ascii control)
\udc84\udc8b\udc90\udc91\udc85\udc9a … .pdf                       cp862, root
\udc81\udc97\udc99\udc84/\udce3\udce5\udce7 … 2024.pdf            cp1255 file in a cp862 dir
7643401/\udc81\udc97\udc99\udc84/\udc84\udc8b\udc90… .pdf         the reported path, all cp862
```

The second line is byte-identical to the filename in the reported traceback. The
PDFs are genuinely scanned (image-only, no text layer) so extraction comes up
empty and `read_file` falls through to the `NamedBytes` branch — the exact path
in the crash.

Stack: backend on sqlite, real Anthropic model (Haiku 4.5), a `network_dir`
agent pointed at the fixture, driven through `POST /api/reports/{id}/completions`.

### Before the fix (`git checkout <base> -- backend/app`)

`POST /completions` → **500**, and the backend log carries the reported failure
verbatim:

```
WARNING  _file_tool_common:attach_drive_file_to_session:825 - persistence failed
         for ??? ?????? 2024.pdf: 'utf-8' codec can't encode characters in
         position 0-2: surrogates not allowed
WARNING  agent_v2:main_execution:5847 - [agent] loop iteration 1 crashed:
         PendingRollbackError("This Session's transaction has been rolled back
         due to a previous exception during flush …")
```

Both completions land `status=error` with that message as their body — the turn
is dead, and iteration 2 dies the same way.

### After the fix

`POST /completions` → **200**. The agent lists, reads and reports on the file,
and the row that used to fail now reads:

| column | value |
|---|---|
| `filename` | `הכנסות לווים + דפי חשבון בנק.pdf` |
| `path` | `uploads/files/<uuid>_הכנסות לווים + דפי חשבון בנק.pdf` |
| `source_ref` | `7643401/בקשה/הכנסות לווים + דפי חשבון בנק.pdf` |
| `content_type` / `source_kind` | `application/pdf` / `connector` |

Same columns and same `source_ref` as the failing INSERT, with a storable
filename and a path that matches it.

### Verified at every layer

- **HTTP** — 500 → 200 on the identical request.
- **Completions** — `status=error` (rollback message) → `status=success` with
  the document's contents.
- **DB** — every `files.filename` UTF-8 encodable; every `files.path` exists on
  disk; zero orphaned rows.
- **Backend log** — `surrogates not allowed` / `PendingRollbackError`: 38
  occurrences before, **0** after.
- **Planner context** — `FilesContextBuilder` renders both the attached PDF and
  its page render under readable Hebrew names.

## Tests

- `backend/tests/unit/test_legacy_filename_persistence.py` — recovery at the
  connector boundary, `storage_safe_name` across charsets.
- `backend/tests/e2e/test_legacy_filename_attach.py` — attach / upload / email
  attachment against the real DB (sqlite and postgres reject lone surrogates
  identically), plus "a failed write leaves the session usable".

Each hunk was reverted individually to confirm the matching tests fail for the
right reason.

---

## Second defect, found by the loop and fixed

`recover_filename` decoded a **whole relative path with one charset**
(`_rel_id` → `recover_filename(rel.as_posix())`), so a path whose segments use
*different* codepages — a cp862 directory holding a cp1255 file, ordinary on a
share that outlived a migration — came out half-mojibake: the segment with more
characters decided the charset for all of them.

```
whole path : בקשה/πστ ε∙δ≡·α 2024.pdf      ← cp862 wins, mangles the cp1255 leaf
per segment: בקשה/דוח משכנתא 2024.pdf       ← correct
```

Reads still worked — `_scan_resolve` matches display forms segment by segment —
so this was display-only. But it is what the model and the user read, and an
unreadable name is not much better than a failed read.

`recover_filename` now recovers a path one segment at a time. Splitting before
decoding is safe while every candidate charset is single-byte: no legacy
character can produce a spurious `0x2F`. Verified live on the same fixture — the
listing name now matches the read name for every entry, and a full agent turn
reads the mixed-codepage file and attaches it as `דוח משכנתא 2024.pdf` with
`source_ref = בקשה/דוח משכנתא 2024.pdf`. Covered by
`test_mixed_charset_path_recovers_each_segment`.

## Sandbox gotchas worth recording

- **`uvicorn --reload` silently missed a `git checkout` restore.** The A/B looked
  like "the fix doesn't work" until the log's line number (`:825` vs the fixed
  file's `:835`) showed the old code still resident. Check the reloader actually
  fired before trusting a before/after.
- **Restarting the backend invalidates stored credentials.** LLM provider keys
  and data-source credentials are Fernet-encrypted with a key that did not
  survive the restart, so both came back as `InvalidToken` — surfacing to the
  agent as the misleading `Failed to construct client:` (empty message). Recreate
  the provider and the data source after any restart, or the failure reads like
  a bug in whatever you were testing.
