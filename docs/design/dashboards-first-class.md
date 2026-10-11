# Dashboards as first-class entities

Status: in progress on `feat/dashboards` — phase 0 and phase 1 done (one release, merged to `main` once complete).

## Goal

Today a dashboard (`Artifact`) belongs to a conversation (`Report`): its link,
sharing, schedule and lifetime are the conversation's. This project makes the
dashboard its own entity:

- **Sharing per dashboard**, and a dashboard link (`/d/{slug|id}`) that does not go
  through the conversation. Old `/r/{report}` links keep working.
- **Draft and Publish**: viewers keep seeing the published version until the owner
  publishes again. Publish freezes the definition and re-runs its queries.
- **Refresh and schedule per dashboard**: drafts refresh as today; the schedule and
  refresh-on-view refresh the published version.
- **Deleting a conversation keeps its published dashboards.**
- **No breakage for existing installations**: one migration converts every existing
  dashboard; what a viewer could open before the upgrade, they can open after it.

## Phases

| # | Phase | User-visible |
|---|---|---|
| 0 | Test tooling: corpus database and the before/after baseline | no |
| 1 | Schema: new tables and columns, nothing reads them yet | no |
| 2 | Running without a conversation (`ExecContext`) | no |
| 3 | Copy engine (draft → frozen published copy) | no |
| 4 | The dashboard holds its own settings and sharing; viewers read through `/d` | API only |
| 5+ | Publish, refresh and schedule, conversation delete, UI, data migration, hardening | yes |

Every phase lands as a PR into `feat/dashboards`, with a draft PR from the branch to
`main` kept open so CI (SQLite and Postgres) runs on every push.

## Phase 0 — the corpus and the baseline

Every later phase is judged against two things built here.

### The corpus

`backend/tests/fixtures/dashboard_corpus/` builds, on a fresh test database, every
kind of conversation and dashboard data an installation can hold today — about 65
conversations, each under a stable key — and 19 kinds of reader, each exercising a
different branch of the access checks.

| Kind group | Examples |
|---|---|
| Dashboard structure | 0/1/many dashboards per conversation; version chains; newest version failed/pending/stopped; rows shaped by `artver01`; deleted versions and dashboards; published widget dashboards; page/slides/doc; embedded files; live resources; `artifact_publications` pins |
| Conversation and sharing | every visibility value on both surfaces; user and group shares; archived and soft-deleted reports; projects (private, org-wide, deleted); run-history (`bow_source_access`); viewer chat threads; test runs; training mode; forks (done, hydrating, stale, retired); Auto; run-as-creator vs viewer with per-viewer results; user-scoped and RLS sources; identity parameters |
| Execution and data | query without report; step without query; draft/error/purged default steps; deleted and missing visualizations; `options_source` by id and by title; `load_step`; entities from a step; thumbnails and screenshots; cron (incl. legacy `''`/`'None'`), subscribers, refresh-on-view off; scheduled prompts; encrypted and plaintext payloads side by side |

Readers: owner, org admin, plain member, direct share, group share, removed group
member, project owner / viewer / group grant / revoked grant, member without
credentials, member with own results, run-history scoped / unscoped, user of another
org, service account, viewer-chat owner, forker,
anonymous.

**How the corpus is written.** Each test follows the same order:

1. `run_migrations` gives a fresh database at head.
2. People, groups, roles, projects and data sources are created through the API.
3. The database is taken back to `MAIN_BASE_REVISION` (main's head when the branch
   was cut). Until this branch adds a migration this is a no-op.
4. Conversations and dashboards are written with SQLAlchemy Core against the
   reflected schema — the shape customers have, not the shape of this branch's models.
   No API creates steps or visualizations, and old shapes cannot be produced by
   today's code, so direct writes are required here (`rows.py` explains this).
5. Upgrade to head: this branch's migrations run over every kind.
6. Live dashboard resources are added through the API.

Everything is deterministic: fixed clock, slugs and emails derived from keys.

### The baseline

`tests/e2e/dashboards/test_visibility_baseline.py` asks the API, for every reader
and every corpus conversation, what they can open and what they get: `/api/r/*`
(including viewer chat), `/api/c/*`, every report-list filter and variant (each card),
`/api/reports/{id}` with its shares, files, fork status, chat agents, schedules, runs
and completions, `/api/queries/*`, `/api/steps/{id}`, `/api/visualizations/{id}`,
`/api/artifacts/*` (with slide previews), `/api/thumbnails/*`, `/api/entities/{id}`,
projects and notifications. It records only reads; exports, `/runtime/*`,
`/r/{id}/run`, `/r/{id}/rerun`, `/viewed`, streams and toggles are left out because
calling them changes data.

Answers are normalised so two builds compare byte for byte, without hiding real
changes:

- every row the corpus writes has a stable name, and ids become those names; an id
  the corpus did not write becomes `<new#n>`, numbered within the answer, so distinct
  new rows stay distinct;
- timestamps, tokens and values the run itself moves (`last_seen`, …) are masked;
- lists keep their order (`visualization_ids`, slide previews and the like are
  positional), except id sets and relationships loaded without an `ORDER BY`, which
  SQLite and Postgres return in different orders;
- server errors are recorded as statuses, not raised.

The baseline is generated on SQLite and must match on Postgres too. It is compared
with golden files in `backend/tests/golden/dashboards/`:

- `<reader>.json` — status and body hash per answer;
- `bodies.json` — every distinct body once;
- `SUMMARY.md` — reader × conversation: status of `/api/r/{id}` and the version the
  page shows. **This is the file reviewed by hand**; each change to it in a later
  phase must be named in that phase's PR with the decision that caused it.

On a mismatch the test prints the reader-file diff and the body diff behind each
changed hash.

### Other checks

- `test_corpus_complete.py` — every kind is really in the database, in its shape.
- `test_alembic_single_head.py` — one alembic head (two heads stop `alembic upgrade
  head` at boot).
- `test_upgrade_from_old_release.py` — a 0.0.560 database (`codeperm01`) with an
  old-shape dashboard upgrades to head and keeps its data.

### Running

```bash
cd backend
TESTING=true uv run pytest -m e2e tests/e2e/dashboards --db=sqlite -n 4
TESTING=true uv run pytest -m e2e tests/e2e/dashboards --db=postgres -n 4   # needs Docker
# Regenerate the baseline on purpose only, without xdist:
BOW_UPDATE_GOLDEN=1 TESTING=true uv run pytest -m e2e tests/e2e/dashboards/test_visibility_baseline.py
```


### Behaviour the baseline records as it is

Found while building the corpus; out of scope for this project, recorded unchanged
so a future fix shows up as a deliberate baseline change:

- `/r` shows the newest version even when it failed or is still pending (empty page).
- Thumbnails are rendered from every visualization of the report, deleted ones
  included, not from the dashboard's `visualization_ids`.
- `options_source` stored by query title is lost in PDF/HTML export and in forks.
- A group share opens `/r` but is refused by `/api/reports/{id}` and `/api/artifacts/*`.
- On `/dashboards`, a card for a conversation shared only as a conversation links to
  `/r`, which returns 404 for that reader.
- `GET /api/steps/{id}` returns 500 for a step with no query (`steps.query_id` NULL,
  left by older code): `StepSchema` requires `query_id`.

## Phase 1 — the schema (`dashfc01`)

One migration, schema only; nothing reads the new structure yet, and the
phase-0 baseline is unchanged byte for byte.

| Object | What |
|---|---|
| `artifact_releases` | The frozen published state: remapped `content`, `status` (publishing/ready/failed), `published_by`/`_at`, theme, thumbnail, pptx, `bow_source_access`, `source_version_id` (SET NULL) |
| `artifact_release_data_source_association`, `artifact_release_file_association` | A release's data sources and files (composite PK, CASCADE both sides) |
| `artifact_shares` | Per-dashboard grants; exactly one of `user_id`/`group_id` (`ck_artifact_shares_one_principal`); CASCADE on all keys |
| `artifacts` +14 columns | Owner, project, `visibility`, globally unique `slug`, run identity, data tab, chat settings, cron, refresh-on-view, last run, subscribers, `published_release_id` (SET NULL). New NOT NULL columns default exactly like their `reports` counterparts. `report_id` becomes nullable |
| `queries`, `visualizations` | `artifact_release_id`; owned by a conversation or a release, never both (`ck_*_single_owner`). `visualizations.report_id` becomes nullable |
| `step_user_results` | `report_id` becomes nullable |
| `reports` | `source_artifact_id` (a viewer chat's dashboard) |

- **ORM**: the models declare all of it. `Report.artifacts` / `Artifact.report`
  name their foreign key (`reports.source_artifact_id` is a second path between
  the tables). The release pointer uses `use_alter` (the two tables point at
  each other).
- **Migration lock**: every replica runs `alembic upgrade head` on boot, so on
  Postgres `alembic/env.py` takes a transaction-level advisory lock (bounded
  wait, 5 minutes) before running migrations; concurrent pods wait, then find
  the database at head.
- **Downgrade** restores main's schema exactly and refuses — with a message,
  never by deleting — when data depends on the new structure (rows without a
  conversation, releases, shares, release-owned copies, or values set in the
  dashboard columns it drops).
- **Tests**: `test_schema_dashfc01.py` checks every column, default, key,
  `ON DELETE`, index and constraint against the plan on SQLite and Postgres;
  model vs database; that pre-upgrade code still inserts dashboards; that the
  round trip restores main exactly while keeping every pre-existing default,
  key and index; one refusal case per kind of dependent data; and two
  concurrent `alembic upgrade head` processes on Postgres.

### Notes for later phases

- SQLite does not enforce foreign keys in this app (no `PRAGMA foreign_keys`),
  so `ON DELETE CASCADE`/`SET NULL` only act on Postgres. Code that deletes
  users, groups, data sources or files must still clear the new rows itself, as
  the SCIM group delete already does for `report_shares`.
- SQLite DDL is not transactional under alembic: a migration that fails midway
  leaves a partial schema. The downgrade's refusal therefore runs before any DDL.
- Considered and not done in phase 1: a `lock_timeout` on the migration's own
  DDL during rolling updates. It would change how every migration behaves, so
  it is a separate decision.
