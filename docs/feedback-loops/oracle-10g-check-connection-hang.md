# Feedback Loop — "connection is ok and a new session is created, but it hangs and doesn't complete connecting in the UI"

A customer added an **Oracle Database 10g Enterprise Edition 10.2.0.4 (64-bit
Windows)** connection. Their DBA saw a session open on the database, but the
add-connection modal stayed on **Check connection → "Connecting…"** with no
error. Their screen recording shows it unchanged for about 50s, and
"Schema discovery" never started. We had no access to the database, so we
couldn't look at `v$session` to see where the login stalled.

What this loop checks:
1. "Check connection" can block with no time limit at all, so any Oracle
   server that accepts a connection and then stops answering produces exactly
   this symptom.
2. After the fix, the same situation ends in an error that says what to do.
3. Working Oracle connections behave the same as before.
4. The new `ORACLE_CLIENT_LIB_DIR` opt-in swaps the Oracle client library for
   one deployment while the default stays the same.

## Root cause (validated)

- **No time limit anywhere on the path.** "Check connection" runs
  `client.atest_connection()` (`backend/app/services/connection_service.py:996`),
  which is `asyncio.to_thread(self.test_connection)`
  (`backend/app/data_sources/clients/base.py:169`). It then reads the entire
  catalog through `aget_schemas()` in `_avalidate_schema_access` (previously
  `connection_service.py:2196`). Neither call has a timeout, and no Oracle
  connect or call timeout is set either. A login the server never finishes,
  or a dictionary query that never returns, blocks the HTTP request forever.
  The modal's `formState.busy` stays true and the user sees the spinner
  indefinitely.
- **Why this customer's server stalls (likely, not proven).** The Docker image
  bundles Oracle Instant Client 19.28 (`Dockerfile:152-183`). Its own comment
  says the 19c client connects to servers 11.2 and newer, and 10.2.0.4 is below
  that. A login that creates a server session and then never completes fits an
  unsupported client/server pairing. We couldn't check `v$session`, so this
  part stays a hypothesis. The other candidate, slow 10g `ALL_*` dictionary
  views during the catalog read, hangs the same request. The fix gives each
  cause its own error message, so the customer's next attempt tells us which
  one it is.

## Loop A — deterministic reproduction (no external services)

A "silent listener" (`tools/agent/oracle_silent_listener.py`) accepts TCP
connections and never sends a byte. From the client's side, that's a login
the server never finishes.

```bash
cd backend && uv sync --frozen --extra dev && mkdir -p db
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest -q tests/unit/test_oracle_connection_check_bounds.py
```

Result on the original code, with the fix stashed and `raising=False`
patching so the tests reach the real behavior:

```
test_probe_against_a_server_that_never_answers_fails_within_its_bound[1]
  → hung until killed (exit 143)
test_catalog_read_that_never_returns_fails_with_the_clients_message[connection|data_source]
  → E assert True is False  (blocked for 30s, then reported success)
test_timed_out_call_has_its_source_statement_cancelled, ...lib_dir_selects_that_client...
  → fail (feature absent)
8 failed, 8 passed  (the 8 passing are the "unchanged behaviour" guards)
```

Standalone, this calls exactly what the UI calls (`atest_connection`):

```
STILL HANGING after 90.1s (harness gave up; app has no limit)
```

The process couldn't even exit afterwards: the stuck worker thread kept it
alive.

## Loop B — full stack through the real modal

`tools/agent/boot_stack.sh` and `seed_org.py` boot the stack. The silent
listener runs on :15210, and `tools/agent/oracle_check_connection_hang.mjs`
fills the Oracle form the way the customer did (service `DWH`) and clicks
**Connect**.

| Leg | Result |
|---|---|
| Original code | `RESULT: STILL_CONNECTING after 100s`, matching the customer's video ([screenshot](../../media/oracle-10g-hang/before-still-connecting-100s.png)) |
| Fix | `RESULT: FAILED_WITH_MESSAGE after 60s`, with the message shown under Check connection and the Connect button enabled again ([screenshot](../../media/oracle-10g-hang/after-clear-error-60s.png)) |
| Fix, thick mode (Instant Client 19.28 loaded through `ORACLE_CLIENT_LIB_DIR`) | `FAILED_WITH_MESSAGE` after 60s |

**Regression against a real database.** Oracle's official
`container-registry.oracle.com/database/free:latest-lite` (23ai) with a
2-table schema:

| Mode | Result |
|---|---|
| Thin, fix | `CONNECTED_AND_INDEXED after 6s` |
| Thick (19.28), fix | `CONNECTED_AND_INDEXED after 34s` ([screenshot](../../media/oracle-10g-hang/regression-real-oracle-23ai-thick.png)) |
| Thick (19.28), original code | `CONNECTED_AND_INDEXED after 34s` (twice). The 34s comes from elsewhere in the save/indexing flow and is the same without this change. |

Inside that flow, the calls the fix bounds are fast: in thick mode the login
probe took 0.1s (bound 60s) and the full catalog read 0.9s (bound 300s).

**Harness gotcha.** `main.py` runs with auto-reload, and its uvicorn worker
processes survive a kill of the parent. Before each leg, kill every
`backend/.venv/bin/python3` process and check that only one generation is
running. Otherwise workers from the previous code version still answer some
requests. Also give each run a unique connection name: a reused name fails
with "already exists" and looks like a hang.

## Loop C — the `ORACLE_CLIENT_LIB_DIR` opt-in, in a container

This uses an Ubuntu 24.04 image (the same base as the product image) with
Instant Client 19.28 installed exactly as `Dockerfile:152-183` does it
(ldconfig). A second client is unpacked at `/mnt/customer-ic`, playing the
operator-supplied one. That's 21.13, because the 11.2 and 12.1 downloads
need an Oracle account login. The image runs the real
`init_thick_mode_if_available` and the real `start.sh` block:

```
1) default (existing customers):   ORACLE_CLIENT_LIB_DIR=(unset) -> thick=True client=(19, 28, 0, 0, 0)
2) opt-in:                          ...=/mnt/customer-ic/instantclient_21_13 -> thick=True client=(21, 13, 0, 0, 0)
3) opt-in with a wrong path:        warning + falls back -> thick=True client=(19, 28, 0, 0, 0)
4) ORACLE_THICK_MODE=0 still wins:  -> thick=False client=thin
```

The libraries an older Instant Client links against (`libnsl.so.1`,
`libaio.so.1`) exist in the runtime base.

## The fix

- `backend/app/data_sources/query_cancellation.py`: new `run_bounded()`. It
  runs a blocking call in a daemon thread and gives up after N seconds with
  `SourceCallTimeout`, and cancels the source statement it was running. It
  uses `asyncio.wait` because on 3.11+ `wait_for`'s `TimeoutError` is the
  builtin one and would mislabel a driver's own timeout (a unit test caught
  this).
- `backend/app/data_sources/clients/oracledb_client.py`:
  - `atest_connection` is bounded by `ORACLE_CONNECT_TIMEOUT_S` (default 60).
    When the bound is hit, it returns a message naming the client-version
    cause and `ORACLE_CLIENT_LIB_DIR`.
  - The client declares `validation_timeout_s` (`ORACLE_VALIDATION_TIMEOUT_S`,
    default 300) with a message pointing at Schema and dictionary statistics.
  - The thin-mode `DPY-3010` error gets a hint appended.
  - `init_thick_mode_if_available` honors `ORACLE_CLIENT_LIB_DIR` and falls
    back to the default libraries if that path doesn't load.
- `backend/app/services/connection_service.py`:
  `_aread_tables_for_validation()` bounds the catalog read of
  "Check connection" for clients that declare a bound. `ConnectionService`
  and `DataSourceService` both use it. Clients without a bound read exactly
  as before.
- `start.sh`: when `ORACLE_CLIENT_LIB_DIR` is set, puts it first on
  `LD_LIBRARY_PATH`, so its dependent libraries resolve from the same
  directory.

None of this applies to indexing or query execution. Both bounds can be
changed per deployment, or disabled with `0`.

## What this proves / regression notes

- A server that accepts a login and never finishes it can no longer hang
  "Check connection". The user gets an actionable error within the bound, and
  any statement still running on the source is cancelled.
- Working Oracle connections (23ai, thin and thick) connect and index the same
  as before. Default library selection is unchanged.
- **Not proven:** that a real 10.2.0.4 server works with an older Instant
  Client. There's no 10g server here, and the 11.2 client sits behind an
  Oracle login. Next step for this customer: put Instant Client 11.2
  (x86-64) on the backend host, mount it (for example at
  `/opt/oracle/instantclient_11_2`), set `ORACLE_CLIENT_LIB_DIR` to it, and
  retry. If it still fails, the error message now says which half stalled.
- Pre-existing and unrelated, found while testing: Oracle foreign keys are
  always dropped, because SQLAlchemy reports lowercase names (`orders`) while
  the client keys tables by uppercase (`ORDERS`). Tracked separately.
