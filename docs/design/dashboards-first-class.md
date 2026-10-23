# Dashboards as first-class entities

Status: in progress on `feat/dashboards` — phases 0–3 done, phase 4 in progress (one release, merged to `main` once complete).

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

## Phase 2 — running without a conversation

### Part A — the execution core takes an `ExecContext`

`StepService._execute_step_code` used to take the `Report` and read its data
sources, files and organization off it. It now takes an `ExecContext`
(`app/services/exec_context.py`): the organization and its settings, whose
credentials open the connections, the data sources and the files. Every
existing caller (`rerun_step`, `run_step_to_user_result`, `read_query`'s
re-execution) builds one with `ExecContext.from_report`, which reads nothing
until the core asks — the data sources only when no prebuilt clients were
passed, the organization only when the caller did not pass it — so each run
reads the same values at the same point as before. The baseline is unchanged.

`ctx.report` remains for the two helpers still scoped to a conversation:
`load_step` resolution and the bow monitoring client. A context without a
report resolves no `load_step` reference and installs no bow client; making
both release-aware belongs with refreshing published dashboards (phase 6),
together with `ExecContext.from_release`.

`test_exec_context.py` checks that `from_report` carries what the core used to
read and loads nothing early; that given organization and settings are used as
they are; that the same clients reach the code with or without a
conversation; that the core runs with no conversation at all (files and the
organization's row limit still apply); that connections are opened with the
context's user; and that a conversation rerun still resolves `load_step`.

The corpus gained two shapes the API allowed before 0.0.583: a query whose
widget sits in another conversation (`widget_elsewhere`) and two queries on
one widget (`shared_widget`). Part B resolves ownership through the widget
first, and these rows pin that it answers as before.

### Part B — who owns a row, and a query without a widget (`dashfc02`)

A published dashboard's copies of queries, steps and visualizations belong to
their release (`artifact_release_id`), with no conversation and no widget.

- **`dashfc02`** makes `queries.widget_id` nullable (`steps.widget_id` already
  was in the database; both models now say so). The downgrade restores NOT NULL
  and refuses, before any DDL, while a query without a widget exists. On SQLite
  each migration's DDL commits on its own, so a downgrade from head to main that
  dashfc01 refuses stops at dashfc01 with dashfc02 already undone (a consistent
  state); Postgres rolls the whole run back.
- **`app/services/row_owner.py`** answers who owns a step or a query: a
  conversation, a release, or no one. The rules reproduce what each caller
  read before, so no existing row changes hands: a step with a widget belongs
  to that widget's conversation, or to no one when the widget has none (as
  `step.widget.report` read); a step without a widget (a copy) to its query's
  conversation, else its release; a query to its own `report_id`, then its
  widget's conversation, then its release. The two builder paths that joined
  the query's widget pass `widget_first=True` and get that widget's
  conversation only, as before. The corpus pins the shapes where these
  differ: a widget in another conversation and a widget with no conversation.
- **Every gate goes through it and refuses what is not a conversation.**
  `guard_route` answers 404 (`report.not_found`) on every route taking a query,
  step or visualization id whose row exists without a conversation; an id with
  no row still gets the route's own answer. Step export and display, the
  conversation rerun, `run_existing_step`, the builder run and preview, the
  viewer run, the step-data policy, the query overlay, the credential-scope
  note, `read_query`, `run_query`, `add_parameter`, e-mail attachments and the
  entity bow client resolve the owner the same way; the builder run refuses
  before it writes, the viewer run before it serves a cached result. A step
  that belongs to no one is withheld, never served; a copy whose code loads
  other steps counts as identity-scoped. Rendered artifact exports are left as
  they are: they receive an artifact version, which always has a conversation
  until phase 4 adds dashboard-level reads.
- **Lists**: `GET /api/queries` and `read_query` never return copies.
- **Report rerun**: a query without a widget falls back to its own latest
  step, never to "the latest step of no widget".
- **Freeze**: `test_widget_lookups_frozen.py` lists the conversation lookups
  through a widget that remain (a conversation's own widgets and steps), each
  with its reason; a new one fails until it goes through `row_owner`.
- **Decision 5 (legacy queries with no `report_id`)**: they now reach their
  widget's conversation in every query-keyed check — `guard_route`'s monitoring
  check, the `/api/queries` visibility filter, the query overlay, the
  credential-scope note, the viewer run's monitoring gate, `read_query`'s gate,
  e-mail attachment matching and identity taint — so that conversation's data
  rules apply to them. A query that reaches no conversation at all (no
  `report_id`, and a widget without one) is refused instead of left unchecked.
  In the corpus one row moved: `credential_scoped` on `GET /api/queries/{id}`
  for `orphans/q_no_report` is computed (`false`) instead of `null`.

Tests: `test_release_copies_invisible.py` (every conversation route from the
OpenAPI schema × every corpus reader: never 2xx, never 500, never the copy's
data, never a change to it, and 404 for readers who see the conversation;
lists; `row_owner` against what each kind of check read before, for every
corpus row, with decision 5 as the only allowed difference),
`test_release_copies_gates.py` (the gates, the services, the agent tools, and
missing ids answering as before), `test_schema_dashfc02.py`,
`test_widget_lookups_frozen.py`.

### Notes for later phases (from phase 3)

- A release copies `applied_params` and `data` as they are, including values
  derived from the publisher's identity and rows of user-scoped or RLS
  sources. The `/d` reader (phase 4) must apply `redact_applied_params` and
  the withholding policy, as Fork does at copy time.
- `load_step` by title in a release resolves among the release's copies only
  once `ExecContext.from_release` (phase 6) gives load_step a release scope.

### Notes for later phases (from part B)

- The daily step-data purge joins through widgets, so it skips copies; copies
  of superseded or failed releases need their own retention rule (phase 5).
- Switching a conversation's run identity deletes viewer results by
  `report_id`; viewer results on copies need the same treatment (phases 4–5).
- Copying a step needs a new `slug` (unique), and a copy's
  `parameters[].options_source.query_id` must point at the copied query (phase 3).
- `resolve_step_data` withholds every copy for now; `/d` needs the dashboard's
  own policy there before it serves data (phase 4).
- Table-usage events must not be recorded for copy runs, or copy code reaches
  the agent's examples (phase 6). Console metrics join through widgets and do
  not count copies.

## Phase 3 — the copy engine

`app/services/dashboard_copy.py` copies rows and rewrites every reference to
them. Publish (phase 5) and the data migration (phase 9) copy one dashboard
version into an `ArtifactRelease`; Fork copies a whole conversation. Nothing
calls the release path yet.

- **What a dashboard needs** (`collect_dashboard_deps`): the queries its link
  shows — what `GET /api/r/{report}/queries?artifact_id=` returns today: live
  queries with a successful default step that own a listed chart, or all of
  them when the version lists none — plus, transitively, every query those
  reach: a filter's option query (by id, else exact title ignoring case, as
  the page resolves it: shown queries first) and every `load_step` target (id,
  slug, then title, as `LoadablesResolver` does — a deleted query's step
  still loads, so it is copied still deleted). Only the dashboard's own
  conversation is read; step data is not loaded for this.
- **Copying rows** (`copy_query_rows`): each query, its default step (a new
  unique slug) and its charts. A deleted chart is copied still deleted: the
  page receives the charts of the queries it shows, deleted ones included.
  The copies' filter references and `load_step` calls point at the copies from
  the first INSERT (ids and slugs are chosen up front, so no UPDATE fires step
  hooks): an id or slug becomes the copy's; a title stays, unless several
  copied steps share it, then it becomes the slug of the copy the draft
  resolves to (the most recent). Literals are found and rewritten through the
  AST, the same walk `extract_loadable_refs` uses.
- **Rewriting content** (`remap_ids`): every copied chart, query and step id
  anywhere in the content (chart list, page code, doc markdown, slides), in
  any letter case. Other ids (files, sources) pass through.
- **Into a release** (`copy_dashboard`): the rewritten content, the source
  version, the conversation's data sources, files, theme and monitoring
  access, and the thumbnail, deck and slide previews copied as the release's
  own files (`release-<id>`; a deck's thumbnail is its first copied preview).
  Previews are resolved like the preview route resolves them (PNGs inside
  `uploads/pptx_previews` only), the deck must be a `.pptx` inside `uploads/`;
  anything else keeps its path and is not copied. Data is copied as it is;
  nothing runs.
- **Fork** uses the same row copy and rewriting with its own scope (every live
  query of the conversation, plus deleted ones a step still loads, copied
  still deleted), widgets, owner and data policy; the code it withholds is
  rewritten the same way before hydration writes it back, and its summary for
  the agent names live queries and charts only. Four things a
  fork used to get wrong are fixed by it: deleted charts and queries came back
  to life; a filter naming its option query by title lost its options; a
  `load_step` by id or slug stopped resolving; query ids and upper-case chart
  ids in page code were not rewritten.

The corpus gained `refs_in_code` (load_step by id and by slug, a query id in
page code, an upper-case chart id, a deleted chart of a live query). Tests:
`test_dashboard_copy.py` copies every live dashboard version in the corpus and
checks it holds what the link shows (plus the hidden queries), that no
reference to a copied row's old id is left, that the data is the same and the
draft untouched; filters, `load_step`, chart order, deleted charts, the empty
chart list, files, and copying twice. `test_fork_copy_fixes.py` pins the four
fork fixes; the existing fork tests pin everything else.

## Phase 4 — the dashboard holds its sharing and settings; viewers read through `/d`

Decisions (with Rachel, 2026-10-12): a dashboard's link is `/d/{id}` (or
`/d/{name}`) from the moment it exists, and shows only its published release;
`/d/{id}/draft` shows the draft to the owner and whoever can view the
project; an editor opening an unpublished dashboard is sent to the draft;
sharing a conversation opens neither; a dashboard is shared only once it is
published; the viewer response carries what the page shows, never the
owner's settings; refresh-on-view waits for phase 6, viewer chat for phase 7,
renaming for phase 8. Old `/r/{report}` links keep working for good (part B).

### Part A — access, sharing and settings on the dashboard

- **One check per door** (`app/services/dashboard_access.py`). `/d`: the
  dashboard exists and is live; the release's monitoring access
  (`bow_source_access`); the owner (`artifacts.owner_id`); whoever can view
  `artifacts.project_id`; then `artifacts.visibility` — `none` (not found),
  `shared` (`artifact_shares`, users or groups), `internal`, `public`. That is
  the conversation check's order. Stricter, being new code: ownership, a
  grant and `internal` need a live membership of the organization, and an
  unknown visibility opens to no one. No ready release: editors get
  `dashboard.not_published`, others `artifact.not_found`. `/draft`: editors
  only, `artifact.not_found` for anyone else whatever the visibility (401 for
  every anonymous caller alike).
- **Routes** (`app/routes/dashboard.py`): `GET /api/d/{ref}` (title, mode,
  release, theme, Data tab and chat switches, last refresh, org branding) and
  `GET /api/d/{ref}/draft`; for the owner, `GET/PUT /api/dashboards/{id}/sharing`
  and `/settings`. The path parameter is `dashboard_id`: the permission
  decorator reads `artifact_id` as a version id.
- **Sharing** (`dashboard_settings_service.set_sharing`): today's rules
  (groups of the org, a monitoring dashboard is never public, a list left out
  keeps its grants, leaving `shared` clears them, in-app notice to new readers
  and the members of new groups, audit) plus: visibility other than `none`
  needs a ready release (`dashboard.share_requires_publish`, 409), and every
  user granted is a member of the org. The list read back holds only grants
  that still count (members, live groups); a stale row opens nothing and goes
  on the next save. The notice links to `/d/{id}`. The owner's controls also
  require the monitoring access the dashboard is built on.
- **Settings** (`set_settings`): run identity, Data tab, chat (enabled,
  sources, model), with today's rules; everything is validated before
  anything changes. "Run on my behalf" is refused when the sources viewers'
  runs read (the release's, else the editing conversation's) include an RLS
  relation (`dashboard.rls_requires_viewer`). Switching it deletes viewers'
  results on the dashboard's release copies. Schedule settings come with
  phase 6.
- **Owner and project**: `new_artifact` sets them from the conversation;
  moving a conversation (bulk move, `PUT /reports/{id}`) moves its dashboards.
  Existing dashboards get them from the data migration.
- **Groups**: deleting a group (RBAC and SCIM) deletes its `artifact_shares`
  rows; the foreign key cascades on Postgres only.
- **`dashfc03`**: `artifacts.legacy_report_id` (unique, no foreign key) —
  which conversation's old `/r` link leads to this dashboard. Filled by the
  data migration; the downgrade refuses while a value is set.
- The conversation's own sharing, settings and `/r` routes are untouched; the
  baseline does not change.

Notes for phase 5 (from the part A review):

- Publish must re-check "run on my behalf" against the new release's
  sources: switching it on refuses RLS, but a later release can add an RLS
  source while it stays on.
- A release that carries monitoring access must not stay public: Publish
  refuses, or sets the visibility back, when the dashboard is public.

Tests: `test_dashboard_access.py` (readers × visibility × project, the draft
door, unpublished and not-ready releases, conversation shares, leaving the
org, monitoring, names, deletion, the response's fields),
`test_dashboard_sharing.py`, `test_dashboard_settings.py`,
`test_dashboard_ownership.py`, `test_schema_dashfc03.py`, and the SCIM case
in `rbac/test_scim_groups.py`. Releases are made with the copy engine until
Publish exists (`dashboard_setup.py`).

### Part B — reading through `/d`

- **Published side** (`app/services/dashboard_data.py`, routes under `/api/d/{ref}`):
  `content` (the release's), `queries` (live copies with a successful
  default step: shown ones and those filters and loaded steps need),
  `queries/{id}/step`, `queries/{id}/export` (the Data tab's CSV/XLSX),
  `files/{id}/embed_token` (files the release's content holds), `previews`
  and `previews/{n}` (the release's PNGs, inside `uploads/pptx_previews`),
  `export/{pdf|pptx|html}`. A row that is not the live release's answers
  `artifact.not_found`, like a missing one.
- **Data policy** (`resolve_release_step`): `resolve_step_data`, asked of the
  dashboard — owner `artifacts.owner_id`, run identity
  `artifacts.shared_run_identity`, the release's data sources (none: the
  organization's the code names, as for a conversation without sources),
  the release's monitoring access; the reader's own run of the copy, then
  the identity taint, then RLS / personal connections. Withheld: no rows, no
  code, identity values redacted. Code visibility follows the reader's role.
- **Exports** (`ReleaseRender`): the release in the shape the export
  services read, with the rows one reader may see per chart and per option
  list (PR #1304's rule for public outputs). Refused (409,
  `dashboard.export_withheld`) for a reader from whom the snapshot as a whole
  is withheld; the owner exports their own view. A deck, its PDF and its
  slide images bake every chart's stored rows in, so they are also refused
  (previews: none) when any copied step is one identity's. "The owner" means
  the owner while still in the organization. A PDF is rendered under a name
  of its own per request and deleted once sent; one render per release at a
  time, as /r renders one per report. Preview images are served from the
  release's own folder only.
- **Draft side** (`/api/d/{ref}/draft/...`): the newest completed version of
  this dashboard and only the queries it needs (the phase 3 collector), with
  the conversation's policy, for editors. Exports and previews of the draft
  stay on the editor's existing routes.
- **Old links**: `GET /api/r/{report_id}/dashboard` names the dashboard
  recorded in `legacy_report_id`, or 404. The `/r` page uses it once `/d`
  exists (phase 8); the data migration fills it (phase 9).
- **Decided** (2026-10-14): a reader who may open a monitoring dashboard may
  export it (on `/r` it is never exported); an anonymous reader of a public
  dashboard may download the Data tab's CSV/XLSX (on `/r` it needs sign-in).
  Both give exactly the rows the page shows that reader.
- **Not in part B**: thumbnails (served for listings; phase 5 must clear a
  release's thumbnail when its snapshot is withheld, as `set_visibility`
  does), viewer runs and filter runs (part C), MCP (part C).

Tests: `test_dashboard_data.py`, `test_dashboard_draft.py`,
`test_dashboard_exports.py` (the browser and the vendored JS are stubbed; the
rows are compared with the step route for the same reader),
`test_legacy_link.py`; `test_release_copies_invisible.py` now also sweeps the
`/d` routes with a copy that is not of the live release.

### Part C — readers run a published dashboard

- **Whole dashboard** (`POST /api/d/{ref}/run`, `dashboard_runs.viewer_run`):
  the release's shown charts re-run for the reader, each into the reader's
  own `step_user_results` row (`report_id` NULL); the copied snapshot never
  changes. Mirrors `viewer_rerun_report_steps` + `run_step_to_user_result`
  over `ExecContext.from_release` (the release's sources and files). The
  owner refreshes through Publish and refresh, not as a reader (400).
- **One chart with filter values** (`POST /api/d/{ref}/queries/{id}/run`,
  `filter_run`): mirrors `run_query_viewer`, with its per-reader cache keyed
  on the values. "View as" belongs to the editor and is refused.
- **Whose credentials** (`run_identity`): the dashboard's "run on my
  behalf"; monitoring and RLS sources always run as the reader; the owner's
  credentials only for members or people the dashboard is shared with, and
  only while the owner is in the organization. Identity values bind the
  reader and cannot be supplied.
- **"Auto" releases** resolve as their conversation would: the dashboard run
  over the sources the credential user may use, the filter run over the
  organization's.
- **load_step** inside a release resolves nothing until phase 6; such a step
  fails alone, the others run.
- **MCP**: `get_visualization` serves a chart of a live release and
  `get_artifact_data` takes a `dashboard_id`, both under the `/d` rules and
  with each reader's rows. The conversation paths are unchanged.

Tests: `test_dashboard_runs.py`, `test_dashboard_mcp.py`.

### Notes for later phases (from phase 4)

- **Phase 9 removes the conversation-keyed path phase 4 duplicated**, with a
  table of every old function and what replaces it, and a check that none is
  still called and that no logic is left in two copies: the `/r` reads,
  exports, viewer run and filter run; conversation-level sharing and viewer
  settings writes; the `/r` access check. The conversation itself (editor,
  `/c`, draft runs) stays. The old `reports` columns go in a later release.
- Syncing with main after PR #1304 (checked in a scratch merge, 2026-10-14):
  `mint_file_token` takes `organization_id` (and binds published-report
  embeds to a report) -- `/d` embeds must pass the dashboard's organization
  and a release binding; a global guard answers a generic 404 for malformed
  ids before the routes; #1304's viewer-scoped exports (`viewer=`) and our
  release reader (`rows_for`) merge into one payload builder; our migrations
  chain after theirs.

## Phase 5 — Publish

Decisions (with Rachel, 2026-10-14): the owner publishes the version open in
the editor (by default the newest that completed; a failed or pending one
cannot be); a slide deck publishes frozen (copied, nothing runs); publishing
runs in the background (`publishing` → `ready` / `failed`), one at a time per
dashboard, a stopped one fails after a while; all or nothing, with the
publisher's connections checked before anything is created; the previous
release is deleted once the new one is live, a failed attempt keeps its row
(the reason) but not its copies; "run on my behalf" over RLS is not blocked
(runs already force the reader); a public dashboard may not publish
monitoring data; the thumbnail is drawn again from the release, none when its
rows are withheld from readers.

### Part A — a release runs whole

Brought forward from phase 6, since Publish runs every copy:

- **`load_step` in a release** finds that release's copies only
  (`LoadablesResolver(release=...)`, `ExecContext.release`), by id, slug, then
  title, as in a conversation, and never the draft's steps or another
  release's. What a copy loads resolves per reader (`resolve_release_step`).
- **The bow monitoring client** runs against a release
  (`install_bow_client(release=...)`): it checks the release's monitoring
  access; Agent Lists follow the release's sources (`release_has_lists`).
  `protect_release` records monitoring access on a release while it is being
  published (flushed, never committed mid-run) and refuses a public
  dashboard. A live release is frozen: a reader's run never changes it.
- **Identity taint** resolves a copy's `load_step` among its release's copies
  (`release_step_lookup`), so a copy that loads another is withheld only when
  what it loads is one identity's slice (phase 2 withheld them all).
- Readers' runs execute what a chart loads first
  (`dashboard_runs.in_dependency_order`).

Tests: `test_release_load_step.py`, `test_agent_lists.py`.

### Part B — Publish

- **Routes**: `POST /api/dashboards/{id}/publish` (`{version_id?}`, 202) and
  `GET` (the live release, the latest attempt, `has_unpublished_changes`).
  Owner only (`publish_reports`).
- **Before anything is created**: the version completed; the publisher still
  reads the conversation's monitoring data, and a public dashboard refuses it
  (`dashboard.publish_public_monitoring`); no other publish runs
  (`dashboard.publish_in_progress`; a stale one is failed first); the
  publisher's clients build for the sources the dashboard's code uses
  (`needed_sources`: named in the code, else all), no query runs
  (`dashboard.publish_connection` with the connection names).
- **dashfc04**: a partial unique index holds at most one `publishing` release
  per dashboard, so two servers cannot both start one; and the four columns
  that referenced `steps.id` without an index get one (deleting a release's
  steps checks each on Postgres).
- **The run** (`dashboard_publish.run_publish`, spawned): every copied default
  step runs with the publisher's credentials, what it loads first, each
  committed (nothing reads a release before it is live). It beats
  (`updated_at`) every minute on a session of its own, also while one step
  runs; one that has not beaten for five minutes is failed by the next
  publish or status read.
- **Go live** is one transaction under the dashboard's row lock:
  compare-and-set to `ready`, `published_release_id` and `last_run_at`, and the
  dashboard's other releases deleted (`delete_release_rows`, in foreign-key
  order, then their files). A dashboard made public meanwhile fails a
  monitoring release instead. **Failure**: `failed` with the reason, copies
  and files deleted, readers keep the live release.

Tests: `test_dashboard_publish.py`, `test_schema_dashfc04.py`.

### Part C — after publish

- The thumbnail is drawn from the live release in the background
  (`ThumbnailService.regenerate_for_release`); none (and the copied one
  removed) when readers may not get its rows or they are monitoring data; a
  deck keeps the one it was published with.
- Readers' own results on the previous release go with it; they run again on
  first open.

### Notes for later phases (from phase 5)

- Phase 6 (refresh, schedule) reuses `run_publish`'s run without the copy.
- Phase 8: the Publish button, the "unpublished changes" label
  (`has_unpublished_changes`) and the failed-publish screen.
- `start_publish`'s in-progress check and the dashfc04 index give the same
  answer; the check saves the connection check, the index holds between two
  servers.

## Phase 6 — refresh and schedule of the published dashboard

Decisions (with Rachel, 2026-10-15): a refresh runs every copy of the live
release and writes all of the new rows at once, or none (the copies keep
their ids; a new release per refresh would change them); the schedule runs as
the dashboard's owner as they are at run time, while still in the
organization; refresh on view keeps the conversation's rule (the owner's
credentials, at most once per five minutes, one at a time; not during a
publish); a dashboard is scheduled only once published; the schedule fires in
the organization's timezone when it has one, else on the server's clock (the
scheduled prompts' rule); subscribers are told on success as today (a link,
now to `/d`); on failure (2026-10-21) the subscribers are emailed that it
failed and the owner gets a Notifications row, no email; a connection each person
signs in to refreshes exactly as today (the owner's sign-in; readers' own
results go, as main's rerun_step drops them -- phase 9 briefly kept them,
then reverted); the conversations' schedules
stay as they are until the data migration.

- **Settings**: `cron_schedule`, `refresh_on_view` and
  `notification_subscribers` on `/api/dashboards/{id}/settings`, with the
  conversation's validation; `""` unschedules and clears the subscribers;
  scheduling an unpublished dashboard was refused here; phase 8 reverses
  that (see there): before its first publish a dashboard's schedule
  refreshes its draft. Saving registers the job.
- **Jobs** (`dashboard_refresh`): `dashboard_{id}`, holding only the id; the
  leader re-registers every published, scheduled dashboard's job at startup
  and removes any other `dashboard_` job. A fire claims the run
  (`claim_scheduled_run`); a dashboard gone, unpublished or unscheduled
  drops its job; an owner no longer in the organization fails the run.
- **The run** is Publish's (`dashboard_publish.run_release_copies`), handed a
  keep-it-for-later callback: what a copy loads reads the refresh's own new
  rows (`ExecContext.fresh_rows`); at the end, under the dashboard's row lock
  and only while the release is still live, every row is written (readers'
  own results on them are deleted, as main's rerun_step does), and `last_run_at` / `last_run_status` /
  `last_run_error` (dashfc05) are set. A failure writes no row and records
  the reason. A published deck is frozen and does not refresh.
- **Routes**: `POST /api/dashboards/{id}/refresh` (the owner's Refresh) and
  `POST /api/d/{id}/refresh` (on view, under the `/d` access check); the
  publish status reports `last_run`.
- **Notifications**: the conversation schedule's inbox row and email with a
  `/d` link; failures go to the owner through
  `automation_alerts.notify_owner_of_failure` (which now takes a `link`).

Tests: `test_dashboard_refresh.py`.

## Phase 7 — a published dashboard outlives its conversation

Decisions (with Rachel, 2026-10-15): deleting a conversation still archives
it; its published dashboards leave it and stay live, its never-published ones
are deleted with it, and unpublished changes stay behind; a preview says which
of the three each dashboard is ("publish and delete" is a phase-8 flow:
publish, wait for success, then delete); "Edit" on a dashboard without a live
conversation starts a new one from the release and moves the same dashboard
into it (owner only); Fork works from a published dashboard with Fork's rules;
readers' chat is per dashboard and per reader, with only the rows they may
see; project delete follows the conversation rule. Also: a conversation is
not deleted while one of its dashboards publishes; a dashboard without a live
conversation does not publish (edit first); a Fork is a new dashboard of the
forker; chat with nothing chosen uses the release's sources and the
organization's model.

### Part A — deleting a conversation (`dashboard_lifecycle`)

- `delete_outcomes` / `GET /api/reports/{id}/delete_preview`: survives,
  loses_changes (published from a version that is not the newest completed),
  or deleted (no live release).
- `settle_dashboards`, in the same transaction as `archive_report`,
  `bulk_archive_reports` and `delete_project`, each dashboard locked as
  `start_publish` locks it: one that is publishing refuses the delete (409,
  a stopped publish failed first); published dashboards get
  `report_id = NULL`; others get `deleted_at`. `leave_project` takes a
  deleted project's remaining dashboards out of it (published) or deletes
  them (never published, conversation gone).
- A Fork from `/d` links no files, carries slides, deck and thumbnail only
  when its reader may see them, needs `create_reports`, and names no
  conversation of the owner's (`forked_from_id` empty; refresh-on-view
  waits for any hydrating fork). Edit makes the live release point at its
  new version (no false "unpublished changes"); two Edits at once give one
  conversation; a version of a conversation the dashboard has left does not
  publish; a dashboard deleted while publishing does not go live.
- The draft door (`open_draft`) and `start_publish` need a live editing
  conversation (`dashboard.edit_first`).

### Part B — "Edit" and Fork from the release

- `release_to_conversation`: a new conversation of the user with the
  release's sources, files, theme and monitoring access, and its queries,
  default steps and charts copied by `copy_query_rows` (the release's copies
  get widgets in the conversation); content remapped, slide previews and
  deck copied to files of its own.
- `POST /api/dashboards/{id}/edit`: the live conversation, else a new one
  with a new draft version of the same artifact
  (`artifact_service.new_version(report_id=...)`) and `artifacts.report_id`
  moved to it; rows copied as they are (the owner's). The agent's tools match
  versions by conversation, so they edit it there.
- `POST /api/d/{id}/fork`: Fork's eligibility against the release's sources
  and Fork's data policy (`fork_service.make_step_overrides`, now shared),
  a new artifact of the forker's; over a personal connection the code is held
  back and `hydrate_fork` runs it as the forker.

### Part C — readers' chat (`dashboard_chat`)

- `/api/d/{id}/chat` (status), `.../chat/completions` (send, list) and
  `.../chat/completions/{id}/stream`: the /d access check, signed in, a live
  member, `artifacts.chat_enabled`.
- Threads: `report_type='artifact_chat'`, `source_artifact_id` = the
  dashboard, `forked_from_id` empty so the /r chat never finds them; roster
  synced every message (`ArtifactChatService.sync_roster`, now shared).
- Context: the live release; in data-only scope its charts with the reader's
  rows (`ReleaseRender`).

Tests: `test_dashboard_lifecycle.py`, `test_dashboard_chat.py`.

## Phase 8 — the UI, as close to today's as possible

Decisions (with Rachel, 2026-10-18): no redesign -- the work is wiring. The
/d page is the /r page; Share and Schedule look as they did; one Publish
button; one small line in the delete dialog; a pencil beside the link for
its name.

- **/d page**: the /r page body moved into
  `frontend/components/dashboard/SharedDashboardView.vue`, read through an
  endpoint map (`frontend/utils/sharedDashboardApi.ts`) for three sources:
  `report` (the conversation's /r, unchanged), `dashboard` (/d) and `draft`
  (/d/{id}/draft, where an unpublished /d sends its editors). The /d meta
  adds what the page reads: `is_owner` (counts_as_owner), `fork_eligibility`
  (`dashboard_lifecycle.fork_eligibility`, the same rule the fork route
  applies), `refresh_on_view`, and `data_sources` -- connection type and
  auth policy only (the sign-in gate's icon). The draft meta adds `general`.
- **Runtime** (`/api/artifacts/{id}/runtime/...`): the conversation's
  sharing still lets its readers in; the dashboard's lets in its /d readers
  too (`dashboard_access.admit_reader`: editors, with the doors' monitoring
  check, or -- once published -- its visibility), also once the conversation
  is gone. The owner is the conversation's, or the dashboard's while a
  member.
- **Publish button** (`PublishButton.vue`, beside Share): publishes the
  version open in the editor; spinner while publishing, a toast with the
  reason on failure, a dot while the open draft is not what readers see.
  Shown to whoever the status call answers (the owner).
- **Share and Schedule** save to the dashboard open in the editor
  (`/api/dashboards/{id}/sharing|settings`) and show its `/d` link. Sharing
  waits for Publish: until then the access list is off, with one line.
- **Schedule refreshes what is seen** (reverses phase 6): an unpublished
  dashboard's schedule refreshes its draft with the conversation's own rerun
  (`ReportService.rerun_report_steps`, this dashboard's queries only), as a
  conversation's schedule does; subscribers get the draft's link
  (`/d/{id}/draft`, editors only), the owner is told on failure. Once
  published, only the release refreshes. `dashboard.schedule_requires_publish`
  is gone.
- **The name in the link** (`artifacts.slug`, `artifact_slug_history` --
  dashfc06, `services/dashboard_slug.py`): lowercase English letters, digits
  and hyphens, 3 to 80, not shaped like an id, not reserved; unique in the
  installation, also against other dashboards' old names. Renaming or
  removing the name keeps the old name leading to the same dashboard (a link
  already sent never stops working; who may open it is the sharing). Set
  through `settings.slug`; a fork has none.
- **Share email**: `POST /api/dashboards/{id}/notify` builds the /d link
  itself (never the page's), and attaches the release's PDF as any reader
  would export it (none when withheld; the file is removed once sent).
- **Fork from /d** records `reports.forked_from_artifact_id` (dashfc07), so
  the editor shows the conversation Fork's banner (linking to /d) and waits
  for hydration the same way.
- **Fork of a conversation** (found in manual testing) copies every dashboard
  in it -- page, slides or doc -- each from its newest completed version, as
  an unpublished dashboard of the forker's; it used to copy only the
  conversation's newest page/slides version, whatever its status. Old versions
  with no tie to their dashboard (same id) count as one per mode, its newest.
- **Exports of a personal dashboard** (found in manual testing): with the
  snapshot withheld from a reader, a page or doc export holds the reader's own
  run's rows (`ReleaseRender.rows_for`), as the page shows them; with no run of
  their own yet it is refused (`dashboard.export_needs_run`). A deck stays
  refused (drawn once, from the owner's rows), and so does the share email's
  PDF. The /d meta's `exports_own_rows` lets the page hide the deck's button.
- **Delete dialogs** name the never-published dashboards that go with the
  conversation (`delete_preview`).
- **Dashboards page**: `GET /api/dashboards`, one card per dashboard, the
  same tabs, search, type filter and paging; SQL narrows to what the /d check
  can admit and each candidate is then admitted by it; the conversation id
  only for editors (the pencil).
- **Errors**: `useErrorMessage` finds catalog messages for dotted codes
  (bracket form), and uses them only when the server sent no message of its
  own.

Depends on phase 9 shipping in the same release: dashboards created before
this code have no `owner_id` (they are missing from the list and the new
Share/Schedule/Publish refuse them) and a conversation's schedule is not
shown in the dashboard's Schedule; the data migration fills both.

Tests: `test_dashboard_page.py`, `test_dashboard_slug.py`,
`test_schema_dashfc06.py`, `test_schema_dashfc07.py`,
`test_dashboard_notify.py`, `test_dashboard_list.py`, additions to
`test_dashboard_refresh.py` and `test_dashboard_lifecycle.py`;
`frontend/tests/unit/useErrorMessage.mjs`.

## Phase 9 — the existing dashboards (in progress)

Research and 17 decisions (with Rachel, 2026-10-19) in the phase-9 research
document; plan in six parts.

- **Part A — sync with main** (merge e5f9969e2): main's viewer-scoped exports
  and the release render share one payload builder; `/d` embed tokens are
  bound to the organization and the release (`rel` claim); an organization's
  full admin opens every published dashboard on `/d`, as main's `/r` does
  (#1400); the golden files follow main (#1400, #1304, the query read gate).
- **Part B — reversed (2026-10-21).** Decision 17 assumed main's refresh
  keeps readers' own results; it does not: `StepService.rerun_step` deletes
  every `StepUserResult` of the step (they no longer match the shared rows).
  Phase 6's delete in `dashboard_refresh._run_and_write` stays.
- **Part C — the corpus before and after.** `migrated_corpus` is the corpus
  upgraded through `dashfc08`; `test_migration_before_after.py` has every
  baseline reader follow the same old link (`/api/r/{report}/dashboard`) and
  open the same dashboards with the same rows. The differences allowed are
  the decided ones (decision 6: unshared archived conversations take their
  dashboards with them).
- **Part D — the data migration** (`dashfc08_existing_dashboards`, Core and
  reflected tables, no app code): per conversation, the dashboard `/r`
  showed gets the conversation's owner, project, settings, schedule and
  link (`legacy_report_id`); if others could open it, it is published from
  the rows it shows (copied, nothing run; readers' own results copied with
  them) and its valid grants copied. Other dashboards become private
  drafts. The conversations' `report_*` jobs are deleted (kept in
  `dashfc08_report_jobs`); a widget-era schedule is removed with its job,
  `reports.cron_schedule` included (decision 7). Every change is in
  `dashfc08_undo`; downgrade restores exactly; a second run adds nothing.
- **Part E — old links and what still read the conversation**
  (`services/dashboard_legacy.py`). Once a dashboard took over a
  conversation's link, the conversation's old artifact sharing admits no one
  (its editors keep access); the dashboard's own sharing decides, on `/d`.
  The conversations list, the project page and the agent's report tools
  read the conversation's dashboards. artver01's per-version dashboards list
  as one card (the newest of a conversation and kind).

- **The conversation's old API on a migrated conversation**
  (`services/conversation_dashboard.py`, approved 2026-10-22). Once the
  migration recorded a conversation's dashboard, the conversation's
  schedule, publish toggle, `PUT status`, artifact sharing, its shares,
  `notify` (dashboard share) and `GET /reports/{id}` act on and answer from
  that dashboard; the conversation's columns are never written again -- they
  are the migration's backup. A conversation made since answers its old API
  exactly as main (the UI never calls it for a dashboard; 59 of main's tests
  encode that); one with no dashboard too. The editor's refresh on open
  follows the dashboard open in it, for every conversation.
  Round-6 review rules (approved 2026-10-22):
  - A schedule whose link shows a deck is read and changed on the page that
    already runs it, else on the page with the newest version -- the
    migration's choice -- so the old API never schedules a second page.
  - The recorded dashboard deleted: the old API refuses
    (`dashboard.conversation_moved`), as the `/r` link leads nowhere; it never
    moves to another dashboard of the conversation, which nobody shared.
  - A refused request saves nothing: artifact sharing checks the settings
    sent with it (`dashboard_settings_service.check_settings`) before it
    shares, and saves them after; `PUT /reports/{id}` applies the status
    before any other field.
  - Refresh on open: only one who may edit the dashboard names it (the
    editor's `artifact_id`), and never a deleted one; anyone else gets the
    conversation's dashboard (a conversation made since: its own column).

#### Old `/r` functions and what replaces them

| Before (reads the conversation) | After |
| --- | --- |
| `/r/{report}` page | Forwards to `/d/{id}` (`GET /api/r/{report}/dashboard`): the dashboard the migration recorded, frozen; for a conversation made since, its newest published page (then a published doc, then the newest unpublished one), the first the reader can open. No dashboard: served as before (widget era). |
| `/api/r/{report}`, `/queries`, `/queries/{q}/step`, `/artifacts`, `/artifacts/{a}`, `/run`, `/rerun`, exports, embed tokens, `/r/{report}/chat*` | `ReportService._check_visibility('artifact_visibility')`: closed to readers once the link was taken over; the conversation's editors (owner, project, org admin) keep them -- the editor's draft run and rerun stay conversation-level. Readers use `/api/d/{ref}/...`. |
| `/api/artifacts/...` with `allow_public` | Same rule in `permissions_decorator`. |
| Files embedded in a shared conversation's dashboard | Through `_check_visibility` (same rule); the release has its own file copies. |
| `bow_source_access.protect_report` (refuses on a public conversation) | The conversation's old public setting no longer counts once taken over; the release has its own guard (`protect_release`). |
| Conversations list: badge (`artifact_visibility`), schedule icon (`cron_schedule`), "Shared"/"Private" filter, scheduled filter, "Shared with me" | From the conversation's dashboards: the most open visibility among its published ones, a dashboard schedule, `dashboard_legacy.counts_as_shared()`, `has_scheduled_dashboard()`, `dashboard_visible_to()`. A conversation without dashboards: its own columns. |
| Project page: automations and their count (`reports.cron_schedule`) | Dashboard schedules (one row per dashboard); a conversation without dashboards keeps its own. |
| Project page dashboard cards, org cards, ForkBanner, editor's 403 handoff (links to `/r/{report}`) | Unchanged links; `/r` forwards. |
| Agent tools `search_reports` (status filter and value), `read_report` (status) | `counts_as_shared()`, as the list. |
| MCP report-scoped reads (`_assert_can_view_report`) | Through `_check_visibility` (same rule); MCP's dashboard path is phase 4 part C. |
| Share window: chat agent candidates (`/reports/{id}/artifact_chat/agents`) | Stays: the candidates are the agents the conversation uses (the draft lives there); the choice is the dashboard's `chat_data_source_ids`. |
| Daily step purge (protects shared conversations by their columns) | Stays: the columns are kept (decision 14); release copies have no widget and are never purged. |
| Conversation fire-time guard and `set_report_schedule` (`report_*` jobs) | Only widget-era conversations can still have one; removed with the old columns. |
| Legacy conversation endpoints (`PUT /reports/{id}` status, publish toggle, schedule, artifact sharing, notify) | Migrated conversation: act on its dashboard (`conversation_dashboard`), never writing the conversation's columns. A conversation made since: as main. |

## Collaboration (collab01) — view/edit roles, several conversations

Built on this design in the same PR stack. Full plan, decisions and DoD:
`docs/feedback-loops/2026-10-23-collaborative-dashboards.md`; endpoints:
`docs/feedback-loops/2026-10-23-collaborative-dashboards-api.md`.

- **Roles.** `artifact_shares.role` is `view` or `edit`; everyone in the
  dashboard's project edits it. `dashboard_access.capabilities()` is the one
  answer to "may this person …" (view, edit, publish, share, settings,
  analytics, run_identity, make_public, transfer); every route and the UI ask
  it. Only the owner makes a dashboard public, switches run-as-creator, or
  transfers it; editors share unless `editors_can_share` is off.
- **Several conversations.** `artifact_conversations` lists the conversations
  editing a dashboard. Joining (`POST /dashboards/{id}/edit`) copies the
  newest draft version into the editor's own conversation; every copied step
  is `pending` with no rows and re-runs as the joiner
  (`hydrate_fork(..., retire=False)`). Rows never cross users — the owner
  rejoining keeps theirs.
- **Context.** The joiner's conversation starts with a summary built in code
  from structure and history (charts, filters, sources, notes, change log) —
  never from rows or the earlier chat. The agent gets it with the notes.
- **Drafts.** One implicit draft (`draft_id` NULL); "Start a separate draft"
  adds a named one (`artifact_drafts`). Versions record their lineage
  (`base_version_id`). Editors preview any version at
  `/d/{ref}/draft?version=N&draft=slug` with their own data (`/draft/run`
  runs every query the version needs, filter options included). Publish
  answers 409 `dashboard.published_since` when someone published since the
  draft started, unless `force`.
- **Releases.** The newest five ready releases are kept; Roll back is instant.
  Scheduled and on-open refreshes run as the last publisher while they can
  still edit, else the owner.
- **Attribution and presence.** Each version stores per-chart fingerprints
  (`elements`, ids masked): "Changed by" and the Changes panel compare
  them. `artifact_presence` holds who is editing (browser heartbeat 15 s,
  30 s TTL) and the charts their agent changed (from each saved version);
  the UI polls it. The edit tool's version guard counts only its own
  conversation's versions.
- **Analytics.** The existing artifact analytics, open to editors; editors'
  own visits are not counted.
