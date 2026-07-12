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

## Phases & status

- [ ] **P0 Setup** — deps (uv/yarn), migrations, S3 access verified, app boots.
- [ ] **P1 Sandbox data** — generate many files under `netdir/` (varied types,
      nesting, a couple of very large text/log files for windowed reads).
- [ ] **P2 Config schema** — add `include_globs` + `index_mode` to
      `NetworkDirConfig` & `S3Config`; keep `index_content` back-compat.
- [ ] **P3 Glob scope + enforcement** — shared glob matcher; filter in listing;
      **enforce at `_resolve`/`_resolve_key`** (read/attach rejected off-glob),
      both clients. Clear error surfaced through the tools.
- [ ] **P4 Windowed read (network_dir)** — `_read_window` parity with S3
      (newline-snap, cursor/eof), wired through `read_file` client + tool.
- [ ] **P5 index_mode behavior** — none=live, metadata=list cache,
      content=+keywords; gate content-search capability on the tier.
- [ ] **P6 Seed** — org/user/enterprise license; create network_dir + s3
      connections; index; link to an agent.
- [ ] **P7 Agent verification** — completion + advanced prompts hitting real
      Anthropic; assert tool calls, indexing, glob-deny, windowed paging on
      both sources. Scripted + repeatable.
- [ ] **P8 UI + Playwright** — screenshots of the connection config (glob +
      index_mode), the scope/preview, and confirmation the tables grid is not
      showing a raw file list. Save PNGs here; surface to user.
- [ ] **P9 Harden & iterate** — fix everything until all checks pass cleanly.

## Test matrix (must all pass)

| Check | local (network_dir) | S3 |
|---|---|---|
| list_files (live) | ☐ | ☐ |
| list_files respects glob | ☐ | ☐ |
| read_file (whole, parsed) | ☐ | ☐ |
| read_file windowed, small | ☐ | ☐ |
| read_file windowed, HUGE file paged to eof | ☐ | ☐ |
| read off-glob file → error/warn | ☐ | ☐ |
| search (content) when index_mode=content | ☐ | ☐ |
| search unavailable/degraded when index_mode<content | ☐ | ☐ |
| attach_file respects glob | ☐ | ☐ |
| index_mode=none → live, no catalog rows | ☐ | ☐ |
| glob change re-scopes without leaking | ☐ | ☐ |

## Notes / decisions carried in

- Scope = selection (no per-file activation); auto-activate flag superseded.
- Config form is schema-driven (`ConnectForm.vue`) → glob/index_mode are
  backend-only additions.
- New `qvd` connector exists on main (adjacent, not in scope).
