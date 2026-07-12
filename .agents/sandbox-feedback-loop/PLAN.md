# Sandbox Feedback Loop — File-Source "Scope, not Catalog" Build

Living plan + progress log for building and verifying the file-connector work
(network_dir + S3) end-to-end in a sandbox. Design rationale lives in
`docs/network-dir-file-ops-plan.md`; this file is the executable plan.

## Goal (definition of done)

Build the agreed model and prove it works **reliably** against a simulated
large local directory AND the real S3 bucket, verified by both automated agent
runs and Playwright UI screenshots:

1. **Glob scope** (`include_globs`) on file connections, enforced as an **access
   boundary** at the resolve chokepoint — tools may touch ONLY glob-matching
   files; anything else errors/warns clearly.
2. **`index_mode`** tier on the connection: `none` (live) / `metadata`
   (list cached) / `content` (list + keyword index), replacing `index_content`.
3. **Windowed read** (`offset`/`length` → `next_cursor`/`total_size`/`eof`)
   working on `network_dir` (aligned with the S3 contract) — incl. very large
   files paged cursor-by-cursor, on BOTH local and S3.
4. **Live vs cached listing** per `index_mode`; read always live.
5. **UI**: config form exposes glob + index_mode (schema-driven); the raw file
   list does NOT appear in the agent tables grid.
6. **Verification harness**: completion + advanced agent prompts that exercise
   every file tool, indexing, glob enforcement, and windowed reads — green on
   both connectors.

## Environment

- Backend: `cd backend && uv run python main.py` (:8000), sqlite dev db.
- Frontend: `cd frontend && yarn dev` (:3000).
- Secrets: `.agents/sandbox-feedback-loop/secrets.env` (gitignored).
- Sandbox dir: `.agents/sandbox-feedback-loop/netdir/` (gitignored) — the
  simulated network share.
- S3: bucket `bowathena14` (docs/, results/, csv, parquet) via provided creds.

## Phases & status  — ALL COMPLETE ✅

- [x] **P0 Setup** — deps, migrations, S3 verified (317 objs), backend :8000.
- [x] **P1 Sandbox data** — 906 files / 52MB under `netdir/`; incl. 44MB /
      600k-line numbered log + 120k-line ndjson; off-glob secrets/.env/.key.
- [x] **P2 Config schema** — `include_globs` + `index_mode` on both configs;
      legacy `index_content` hidden; `select` renderer added to ConnectForm.
- [x] **P3 Glob scope + enforcement** — shared `_file_source_common` matcher;
      enforced at `_resolve`/`_resolve_key`; filtered in listing; both clients.
- [x] **P4 Windowed read (network_dir)** — `_read_window` parity with S3.
- [x] **P5 index_mode behavior** — none=live (0 catalog rows), metadata=list,
      content=+keywords. + cheap_live_listing per-connection live listing.
- [x] **P6 Seed** — enterprise org/user; 3 connections (content/live/S3) + agent
      + report; indexed (579 + 0 + 4 catalog rows as designed).
- [x] **P7 Agent verification** — `verify_tools.py` 21/21 + `verify_agent.py`
      9/9 (real Anthropic: completion, glob-deny surfaced, 44MB paging).
- [x] **P8 UI + Playwright** — screenshots of both config forms (glob +
      index_mode) + live report completion showing the glob-denial error.
- [x] **P9 Harden** — fixed 2 real bugs found (cross-connection list/search
      leak; empty non-recursive root listing). Re-verified green.

## Test matrix — ALL PASS ✅ (verify_tools.py 21/21, verify_agent.py 9/9)

| Check | local (network_dir) | S3 |
|---|---|---|
| list_files (live, per-connection) | ✅ | ✅ |
| list_files respects glob (no leak) | ✅ | ✅ |
| read_file (whole, parsed) | ✅ | ✅ |
| read_file windowed, small | ✅ | ✅ |
| read_file windowed, HUGE file paged to eof (600k lines) | ✅ | n/a (S3 windowed ✅) |
| read off-glob file → error surfaced to model/UI | ✅ | ✅ |
| search (content) when index_mode=content | ✅ | ✅ |
| search scoped to connection (no cross-conn leak) | ✅ | ✅ |
| index_mode=none → live, 0 catalog rows | ✅ | — |
| newline-snap + cursor continuity | ✅ | ✅ |

## Known follow-ups (not blockers)

- Scheduled incremental refresh (`reindex_interval_*`) per index tier — hooks
  exist on Connection; wiring the tier into the scheduled job is future work.
- UI: retire the tables-grid for file connectors in favor of a scope
  configurator + `/files` browser (design agreed; not built here).
- Auto-activate flag on DataSource (superseded by scope-as-selection; seed
  activates file tables directly for now).

## Notes / decisions carried in

- Scope = selection (no per-file activation); auto-activate flag superseded.
- Config form is schema-driven (`ConnectForm.vue`) → glob/index_mode are
  backend-only additions.
- New `qvd` connector exists on main (adjacent, not in scope).
