# Plan — Report, artifact and file retention (full delete)

Status: **implemented** (2026-11-26). Proof: `docs/feedback-loops/2026-11-26-data-retention.md`.

Changes from the plan, as built:
- Off is stored as `0` (the settings `FeatureConfig` turns a `None` value into
  `True`) and shown as an empty "Keep forever" field; the API accepts empty.
- The purge walks the schema with a per-reference policy
  (`app/services/retention/purge.py`) instead of a hand-written delete order,
  and a unit test fails when a reference has no policy.
- S7: a report whose deletion would take a dashboard that must stay (published,
  shared, scheduled, embedded, edited inside the window) is **kept whole**, not
  deleted around the dashboard: the dashboard's versions reference it.
- The preview runs the same selection and dashboard check as the night.
- Not enterprise-gated; starred items expire; the audit entry has counts only;
  no owner email before deletion.

## Goal

An org admin sets, in org settings:

| Setting | Default | Meaning |
|---|---|---|
| `report_retention_days` | empty (off) | Reports with no activity for N days are **deleted for good**, with everything a run leaves: completions, agent executions, plans, plan decisions, tool executions, completion blocks, context snapshots, widgets, steps, queries, sub-agents, notes, mentions, check-ins, and files only that report uses. |
| `artifact_retention_days` | empty (off) | Artifacts (dashboards/pages) not touched for N days are deleted for good: versions, releases (with their queries/steps), resources, shares, drafts, viewer chat threads. |
| `file_retention_days` | already exists, `0` = off | Becomes "empty = off" too (`0` still accepted). Deletes the bytes reliably (retry failed blob deletes). |

"Retention" = **hard delete**. No archive step, no soft delete, nothing to
restore. Only an audit log entry is left behind (counts, not content).

## What exists today (validated)

- File retention: `app/services/file_retention_service.py`, purge via
  `file_service.purge_files` (`file_service.py:772`), scheduled
  `main.py:680` (03:45), validated `organization_settings_service.py:337`,
  setting `organization_settings_schema.py:489`, UI key list
  `frontend/pages/settings/ai_settings.vue:293`, test
  `tests/e2e/rbac/test_file_retention.py`.
- Gap: blob delete is best-effort and never retried (`file_service.py:794`).
- No hard delete of reports exists. Archive = `status='archived'`
  (`report_service.py:2159`); artifact delete is soft (`artifact_service.py:505`).
- Most FKs into `reports`/`completions`/`agent_executions`/`artifacts` have no
  `ON DELETE CASCADE`; SQLite runs without FK enforcement, so a missed table
  orphans silently there and fails only on Postgres.
- Rewound completions are hidden from ORM SELECTs (`completion.py:296`);
  purge must use `include_rewound=True`.
- In-repo hard-delete precedent with FK hygiene: release step purge
  `dashboard_publish.py:893-916`.
- The UI renders a numeric input only for `typeof value === 'number'`
  (`frontend/components/settings/AiSettingRow.vue:23`); `null` would render a
  text box.

## Decisions (recommended defaults — confirm before Phase 1)

1. **Clock.** Reports: `Report.last_activity_at` (indexed), falling back to
   `created_at`. Artifacts: max of `updated_at`, `content_updated_at`,
   `last_run_at`, latest release. Files: `created_at` (unchanged).
2. **Never expire** (report): scheduled (`cron_schedule` or active
   `ScheduledPrompt`), has an active webhook, shared/published
   (`artifact_visibility`/`conversation_visibility` ≠ `none`,
   `conversation_share_enabled`, `status='published'`), `report_type='test'`
   (eval runs require `head_completion_id`), any `AgentExecution` with status
   `in_progress`, owns a published dashboard. Starred: **expire** (open question).
3. **Never expire** (artifact): published release, visibility ≠ `none`,
   `cron_schedule`, a host application (embed) points at it.
4. **Keep the row, drop the link:** `llm_usage_record.report_id` (billing),
   instructions/builds' `agent_execution_id`, eval `source_*`/`origin_report_id`,
   `instruction_usage_event`, `instruction_feedback_event`, `table_stats`,
   `entity.source_step_id`, other reports' `forked_from_id`/`source_artifact_id`.
5. **Licensing:** not enterprise-gated (like file retention). Open question.
6. **Range:** empty/0 = off, otherwise 1–3650.

## Deletion order (reports)

Leaf first, per batch of report ids, one transaction per batch:

1. Agent executions of the batch (incl. rewound, incl. children via
   `parent_agent_execution_id`): null `instruction(_build).agent_execution_id`,
   `eval.*agent_execution_id`, `note.agent_execution_id`; delete
   `email_delivery_claims`, `completion_blocks`, `tool_executions`,
   `plan_decisions`, `context_snapshots`, sub-agent rows/messages, the two
   cascading link tables; then `agent_executions`.
2. Completions: null `eval.source_completion_id`,
   `report_context_state.covers_until_completion_id`, `artifacts.completion_id`
   (if surviving); delete `completion_feedback`, `mentions`, `plans`,
   `officejs_pending_results`, `agent_checkins`; break `parent_id` self-ref;
   delete `completions`.
3. Report content: `step_user_results`, `table_usage_events`/
   `table_feedback_events` (step_id NOT NULL → delete), null
   `queries.default_step_id`, then `steps`, `visualizations`, `queries`,
   `widgets`, `text_widgets`, `dashboard_layout_versions`,
   `report_context_state`, `tool_confirmations`, `notes`, `prompt_runs`,
   `scheduled_prompts`, `webhooks`, `report_shares`, `report_stars`,
   `report_views`, data-source links, folder links, unpublished artifacts
   (via the artifact purge), `artifact_conversations`.
4. Files used **only** by these reports → `purge_files` (bytes via the
   blob retry list).
5. Null the keep-the-row links (decision 4); delete `reports`.
6. Audit: one `retention.reports_purged` entry per org per run with counts.

Artifacts: versions → release queries/steps (reuse `dashboard_publish`
release purge) → releases → resources/file bindings (files then fall to
`purge_files` if no one else uses them) → shares, slug history, drafts,
presence, refresh runs, access requests, stars, activity, list-seen → viewer
chat threads (`report_type='artifact_chat'`, via the report purge) → artifact.

## Phases

### Phase 0 — Build the loop first (before any feature code)

Follow `.agents/skills/sandbox-feedback-loop`. Doc:
`docs/feedback-loops/2026-11-26-data-retention.md`.

**Loop A — deterministic (pytest, no LLM).** New `tests/e2e/retention/`:

- `census.py` helper (test utility, not product code):
  - `row_census(db, org_id)`: row count per table for the org (walks
    `Base.metadata`, joins through `org_scope_guard.org_columns_of`).
  - `orphan_census(db)`: for **every** FK in `Base.metadata`, count child rows
    whose non-null FK points to a missing parent. Must be `{}` after any purge.
  - `blob_census(paths)`: which file paths still exist on disk / object storage.
- Data: the `dashboard_corpus` fixture (every kind of conversation and
  dashboard) **plus** a new `full_run` fixture that writes one conversation
  with every run-tree table populated (two completions, parent+child
  execution, plan decision, tool execution, blocks, snapshot, step, sub-agent,
  note, mention, feedback, check-in, usage record, upload + tool-output file,
  rewound completion, instruction built from the run, eval case sourced from it).
- **Baseline run on current code (must FAIL for the right reason):**
  `PUT /api/organization/settings {report_retention_days: 30}` → 400/ignored;
  backdate → run `apply_report_retention()` → `ImportError`/nothing deleted.
  Record the output in the doc.
- **FK coverage guard** (`test_retention_fk_coverage.py`): enumerate every FK
  in metadata targeting `reports`, `completions`, `agent_executions`,
  `steps`, `widgets`, `queries`, `artifacts`, `artifact_versions`,
  `artifact_releases`, `files`; assert each is listed in the purge's
  handled-FK registry (delete / set-null / exempt-by-design). Adding a model
  without handling it fails CI.

**Loop B — live sandbox (real stack, stub LLM).**

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --demo
uv run python ../tools/agent/stub_llm.py &            # deterministic tools
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
uv run python ../tools/agent/seed_retention.py > /tmp/retention_manifest.json   # new
uv run python ../tools/agent/verify_retention.py /tmp/retention_manifest.json   # new
```
(DevBox: `bow-stack status`, skip boot/seed, `source tools/agent/devbox_env.sh`.)

`seed_retention.py` (new) drives **real** conversations through the API so the
run tree is what production writes, then backdates selected rows with SQL and
prints a manifest of expected outcomes:

| # | Scenario | Backdated | Expected |
|---|---|---|---|
| S1 | plain chat, 2 turns, tool output files | 60d | deleted |
| S2 | plain chat | 10d | kept (inside window) |
| S3 | old chat, new message yesterday | created 60d, activity 1d | kept |
| S4 | scheduled report (cron) | 60d | kept |
| S5 | report with active webhook | 60d | kept |
| S6 | publicly shared conversation | 60d | kept |
| S7 | report with published dashboard | 60d | report deleted, dashboard kept |
| S8 | report with unpublished dashboard | 60d | both deleted |
| S9 | rewound turns | 60d | deleted incl. rewound rows |
| S10 | instruction learned from the run | 60d | report deleted, instruction kept (link nulled) |
| S11 | eval case from the run; eval test report | 60d | case kept; test report kept |
| S12 | upload shared by S1 and S2 | 60d | file kept (S2 uses it) |
| S13 | run in progress (slow stub) | 60d | kept this night |
| S14 | standalone dashboard unpublished, untouched | 60d | deleted (artifact retention) |
| S15 | published dashboard with viewer chat threads | 60d | kept |
| S16 | old upload, file retention 30 | 60d | deleted, blob gone |
| S17 | blob delete fails once (chaos: read-only dir) | 60d | row gone, blob retried next run, then gone |

`verify_retention.py` (new): row/orphan/blob census before → set settings via
API → run the jobs (`apply_report_retention`, `apply_artifact_retention`,
`apply_file_retention`) → census after; assert manifest, zero orphans, deleted
reports/dashboards return 404 in API and UI (Playwright via
`tools/agent/capture.mjs`), kept ones still render, usage page totals
unchanged, audit entry present. Run twice: second run deletes nothing
(idempotent).

### Phase 1 — Settings and UI

- Schema: `report_retention_days`, `artifact_retention_days`
  `FeatureConfig(value=None, …)` in the `workspace` section; validation shared
  in `app/services/retention/settings.py` (`days_from_config`: None/""/0 → off).
  `file_retention_days` accepts empty too.
- `AiSettingRow.vue`: nullable-number setting → number input, placeholder
  "Keep forever", empty saves `null`.
- Inline warning under the inputs: "Deleted for good, can't be restored", plus
  a preview count from a new read-only `GET /api/organization/retention/preview`
  (what tonight's run would delete). `manage_settings` permission.
- i18n: `locales/*.json` all files (en/es/he required; follow `localization` skill).
- Evidence: `ui-evidence` skill, before/after screenshots en + he (RTL).

### Phase 2 — Shared purge infrastructure

- `app/services/retention/` package: `settings.py`, `registry.py` (handled-FK
  registry the guard test reads), `blobs.py`.
- Blob retry list: new table `pending_blob_deletes(path, attempts, last_error)`
  + migration; `discard_file_bytes` records failures there; nightly job
  retries. Used by all three retentions.

### Phase 3 — Report retention

- `app/services/retention/reports.py`: `expirable_reports(org, cutoff)`
  query (decision 2), `purge_reports(db, ids)` (order above, batches of 100,
  commit per batch), `apply_report_retention()`, `purge_expired_reports()` with
  `claim_scheduled_run`. Schedule 04:00 in `main.py`.
- `purge_reports` is a reusable service (a future "delete forever" button
  calls the same function).

### Phase 4 — Artifact retention

- `app/services/retention/artifacts.py`, same shape; reuse the release purge
  from `dashboard_publish.py`. Schedule 04:15. Files freed here go to
  `purge_files` directly.

### Phase 5 — File retention hardening

- Empty = off; blob retry list; files freed by phases 3–4 deleted at once,
  not left for file retention.

### Phase 6 — Verify

- Loop A on `--db=sqlite` and `--db=postgres` (Postgres catches FK misses).
- Loop B full manifest, twice.
- Scale: seed 10k reports (`seed_long_report.py`-style), time one night's run,
  confirm batches commit and the app stays responsive.
- Multi-worker: two processes run the job, only one claims it.
- `uv run pytest tests/e2e/rbac/test_file_retention.py` still green.

### Phase 7 — Ship

- Feedback-loop doc with before/after output.
- `docs-update` skill (docs.bagofwords.com retention section).
- `release-notes` skill (VERSION + CHANGELOG).
- PR per `.claude/templates/PR_DESCRIPTION_STANDARD.md`.

## Open questions

1. Starred reports: expire or keep?
2. Enterprise-gate the new settings?
3. Should the audit entry list deleted report titles (useful) or only counts
   (titles are customer content)?
4. A grace notice to owners (email N days before deletion)? Not in this plan.
