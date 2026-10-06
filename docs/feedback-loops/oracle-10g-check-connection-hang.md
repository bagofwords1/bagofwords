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
3. Working Oracle connections, **including a real Oracle 10g**, behave the
   same as before.
4. The new `ORACLE_CLIENT_LIB_DIR` opt-in swaps the Oracle client library for
   one deployment while the default stays the same.

## Root cause (validated)

- **No time limit anywhere on the path.** "Check connection" runs
  `client.atest_connection()` (`backend/app/services/connection_service.py:996`),
  which is `asyncio.to_thread(self.test_connection)`
  (`backend/app/data_sources/clients/base.py:169`). It then reads the entire
  catalog through `aget_schemas()` in `_avalidate_schema_access` (previously
  `connection_service.py:2196`). Neither call has a timeout, and no Oracle
  connect or call timeout is set either. A login that never completes, or a
  dictionary query that never returns, blocks the HTTP request forever. The
  modal's `formState.busy` stays true and the user sees the spinner
  indefinitely.
- **The Oracle version is NOT the cause (disproved on a real 10g).** We first
  suspected the bundled 19c Instant Client, since Oracle's support matrix
  lists 11.2+ servers. Against a real **Oracle 10g XE 10.2.0.1** (the
  `dragonbest520/oracle-xe-10g` image), using the client set up the same way
  the product image does:

  | Path | Result |
  |---|---|
  | Thin mode | `DPY-3010` error in 0.0s, a clear error rather than a hang |
  | Thick, bundled 19.28, raw driver | connected in 0.4s, query OK |
  | Thick 19.28, `OracledbClient.test_connection()` | success in 0.2s |
  | Thick 19.28, `get_schemas()`, 2 tables | 0.3s |
  | Thick 19.28, `get_schemas()`, 1,502 tables with FKs and comments | 0.8s |
  | Full UI (add-connection modal) | connected and indexed ([screenshot](../../media/oracle-10g-hang/real-oracle-10g-connected.png)) |

  So our stock setup works with 10g, and the customer's hang comes from their
  environment.
- **Leading hypothesis: the network path from the backend to the database.**
  The customer's DBA sees a session created (the login reaches the server),
  and then the client never hears back. That's typical of a firewall or load
  balancer that passes the listener port but blocks or inspects the rest of
  the session. Common cases:
  - shared server or Oracle-on-Windows setups that hand the login to a second
    port;
  - "SQL*Net inspection" on a firewall.

  Their SQL Developer works, but it likely runs on a different network
  segment from the backend. This is a hypothesis we can't check without their
  network; the new error message points them at it.

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
| Fix, thick mode, **real Oracle 10g XE** (`dragonbest520/oracle-xe-10g`) | `CONNECTED_AND_INDEXED` |

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
    When the bound is hit, it returns a message pointing at the network path
    between the backend and the database.
  - The client declares `validation_timeout_s` (`ORACLE_VALIDATION_TIMEOUT_S`,
    default 300) with a message pointing at Schema and dictionary statistics.
  - The thin-mode `DPY-3010` error gets a hint appended (use the Docker image,
    which bundles Instant Client, so thick mode is on).
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

## Follow-up: the customer's session was ACTIVE, and the optional SDU field

The customer's DBA reported our session as **ACTIVE**. An ACTIVE session is
running a statement, so the login had completed and the hang is in the
table-list read that "Check connection" does next.

**A huge catalog does not reproduce it.** On the real 10g we built 25,090
tables and 473,588 columns and deleted the dictionary statistics.
"Check connection" read REPOS2000's 4,001 tables in **3.8s**:

| Step | Time |
|---|---|
| Login probe | 0.2s |
| Columns and comments query (108,002 rows) | 2.8s |
| Foreign-key reflection | 2.2s |

**Leading hypothesis: a network that drops large packets.** The customer runs
the backend on **Kubernetes**, where overlay networks (VXLAN or IP-in-IP)
reduce the usable MTU. Login uses small packets and succeeds; large result
packets disappear, and the server sits ACTIVE on
`SQL*Net more data to client`. Documented cases:

- Oracle's own GitHub has an Instant Client hanging over Docker networks
  because of MTU
  ([oracle/docker-images#1153](https://github.com/oracle/docker-images/issues/1153)).
- The standard workaround is a smaller Oracle Net SDU
  ([zeddba](https://blog.zeddba.com/2018/07/20/session-using-a-database-link-hangs-on-sqlnet-more-data-from-dblink/)).

This is not confirmed on the customer's cluster.

**The fix: optional `sdu` field on `OracleConfig`.** It appears in the form as
"Packet size (SDU)", blank by default, and accepts 512 to 2,097,152; a blank
input means not set.

- When set, `OracledbClient._connect_args` adds `(SDU=n)` to an explicit
  connect descriptor.
- When unset, it returns `{}` exactly as before, so every existing connection
  is byte-for-byte unchanged.
- The two "Check connection" timeout messages now suggest setting it.

Verified against the real 10g in thick mode, through `OracledbClient`, with the
negotiated SDU read from the Oracle Net client trace (`nsconneg`):

| Connection | Connect args | Negotiated SDU | Connection test + 4,001-table read |
|---|---|---|---|
| SDU blank (existing connections) | `{}` | 2048 (the 10g default) | OK |
| SDU = 1400 | `(DESCRIPTION=(SDU=1400)…)` | **1400** | OK |

Both settings also connect and index all 4,001 tables through the real modal
([screenshot](../../media/oracle-10g-hang/sdu-1400-real-oracle-10g-connected.png)).

Unit tests (`tests/unit/test_oracledb_thick_mode.py`) cover:
- legacy stored configs with no `sdu` key;
- blank values;
- the descriptor shape, including SDU together with TCPS;
- out-of-range rejection.

**Limits.** A smaller SDU usually avoids oversized packets, but the kernel can
still merge writes into full-size segments. Real path-MTU problems may also
need a network fix: the CNI MTU, MSS clamping, or allowing ICMP "fragmentation
needed". The DBA's wait event (`SQL*Net more data to client`) is the
confirmation.

## What this proves / regression notes

- A server that accepts a login and never finishes it can no longer hang
  "Check connection". The user gets an actionable error within the bound, and
  any statement still running on the source is cancelled.
- Working Oracle connections (23ai, thin and thick) connect and index the same
  as before. Default library selection is unchanged.
- Our bundled setup connects to and indexes a real Oracle 10g, so the
  customer doesn't need a special client. `ORACLE_CLIENT_LIB_DIR` stays as a
  general escape hatch but isn't the fix for this report.
- **Next step for this customer:** deploy this build and retry. If it fails
  with the login-timeout message, the network path is the problem: have their
  network or DBA team check the firewall or load balancer between the backend
  host and the database (allowed ports, SQL*Net inspection, shared-server or
  port-redirect setups). If it fails with the table-list message, set Schema
  or gather dictionary statistics.
- Pre-existing and unrelated, found while testing: Oracle foreign keys are
  always dropped, because SQLAlchemy reports lowercase names (`orders`) while
  the client keys tables by uppercase (`ORDERS`). Tracked separately.
