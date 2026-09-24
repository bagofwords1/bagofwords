# Feedback Loop — "Last checked" shows the wrong time in Manage connection

The **Manage connection** modal's *Connection status* card reads
`Last checked · 9/23/2026, 9:27:12 AM` with a time that is off by the viewer's
UTC offset. The timestamp is right after you click **Test connection**, but it
is wrong once the modal is reopened or the page is reloaded.

## Root cause (validated)

- `backend/app/services/connection_service.py:1064` / `:1085` store
  `last_connection_checked_at = datetime.utcnow()`, a **naive** UTC datetime
  (`backend/app/models/connection.py:29` is a plain `DateTime` column).
- `backend/app/routes/connection.py` serialized it with a bare `.isoformat()`,
  which gives `"2026-09-23T15:31:02.733996"` with **no offset**.
- `frontend/components/EditConnectionModal.vue:30` renders
  `new Date(checkedAt).toLocaleString(locale)`. The JS `Date` parses an
  offset-less date-time string as **local** time, so the UTC wall-clock is
  displayed as if it were local.
- Why it looks right right after a test: the modal first uses its own
  `testedAt = new Date().toISOString()` (`EditConnectionModal.vue:102`), which
  is `Z`-suffixed. Only the server value is wrong.
- Same defect, same file: `last_synced_at` (connection list, create, detail,
  update) and `next_retry_at`. All are naive `utcnow()` values.
  `_iso_utc()` in the same file already existed for the indexing timestamps.
  It was just not used for these fields.

## Loop A — deterministic reproduction (no external services)

Regression test `test_connection_timestamps_are_explicit_utc` in
`backend/tests/e2e/test_connection.py`. It creates a SQLite (chinook)
connection, runs a system connection test and a schema refresh, then asserts
that **every** timestamp on the detail and list payloads carries an offset and
is the UTC instant it was recorded at.

```bash
cd backend
TESTING=true BOW_DATABASE_URL='sqlite:///db/app.db' \
  uv run pytest tests/e2e/test_connection.py -k explicit_utc -q -p no:warnings
```

Before the fix:

```
E   AssertionError: detail.last_connection_checked_at='2026-09-23T15:26:24.612894' has no UTC offset
1 failed
```

## Loop B — live UI confirmation (local stack, no credentials)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
# create a sqlite connection on tests/config/chinook.sqlite, POST /api/connections/{id}/test
node media/pr/connection-last-checked-utc/capture.mjs before.png America/New_York
```

`capture.mjs` logs in and opens Agents → Connections → *Manage connection* in
a browser pinned to `timezoneId`. It prints what the modal rendered next to the
API value.

| | API `last_connection_checked_at` | Browser now | Modal shows |
|---|---|---|---|
| Before (New York) | `2026-09-23T15:31:02.733996` | 11:34:21 AM | **3:31:02 PM** (4 h in the future) |
| After (New York) | `2026-09-23T15:39:04.801041Z` | 11:44:25 AM | **11:39:04 AM** |
| After (UTC) | `2026-09-23T15:39:04.801041Z` | 3:44:43 PM | 3:39:04 PM |

Screenshots: `media/pr/connection-last-checked-utc/{before-new-york,after-new-york,after-utc}.png`.

## The fix

`backend/app/routes/connection.py`: `last_connection_checked_at`,
`last_synced_at` (all 4 serializers) and `next_retry_at` now go through the
existing `_iso_utc()` helper. It appends `Z` to naive values and normalizes
tz-aware ones. Frontend consumers (`EditConnectionModal`,
`ConnectionDetailModal.checkedAgo`, `AgentConnectionsModal`) all use
`new Date(...)`, and none add their own `Z`, so there's no double suffix.

After: Loop A passes, and so do `tests/e2e/test_connection.py`,
`test_connection_indexing.py` and `test_connection_auto_reindex.py` (18 passed).

## What this proves / regression notes

- The contract is "connection endpoints emit explicit-UTC timestamps". It is
  asserted for every timestamp field that is set, on both detail and list, so
  a new field serialized with a bare `.isoformat()` will fail it.
- `last_checked_at` on the per-user status (`data_source_schema.py:113`)
  already used `OptionalUTCDatetime` and was not affected.
- Sandbox note: uvicorn `--reload` hung after the file change (health 000).
  Killing the process group and restarting with the same
  `BOW_ENCRYPTION_KEY` fixed it.
