# Code cleanup audit — 2026-10-27

Research only; nothing was changed. Every claim below was verified by
import-reachability analysis (AST walk from `main.py` / `bow-eval.py` /
`app/ee/routes.py`, with the dynamic loaders — `app/ai/registry.py`,
`app/ai/tools/implementations/__init__.py`, `resolve_client_class` —
accounted for), plus whole-repo grep for string/kebab-case/dotted references.

Baseline: 2,252 backend `.py`, 403 `.vue`, 271 alembic revisions, 203 MB of
tracked media.

---

## 0. The area you already named (for sizing only)

Widget / dashboard-layout / visualization surface, so you know what you're
comparing against:

| Layer | Files | ~LOC |
|---|---|---|
| Backend models + schemas + services (`widget`, `text_widget`, `dashboard_layout_version`, `visualization`, `dashboard_layout_engine`) | 11 | 3,065 |
| AI side (`widget_context_builder`, `dashboard_designer`, `create_widget`, `create_dashboard` impl+schema) | 5 | ~1,900 |
| Frontend (`dashboard/text/TextWidget`, `CreateWidgetTool`, `ToolWidgetPreview`, `pages/dashboards`, `Render{Count,Table,Visual}`) | 7 | 3,638 |

Three blocks in `backend/app/project_manager.py` are already marked
`# LEGACY (widget-based)` — lines 332, 698, 756 — in a 2,029-line file.
`/dashboards` is still wired into the main nav (`frontend/layouts/default.vue:834`)
and the home quick-links (`frontend/pages/index.vue:297`), so retiring it is a
nav change, not just a delete.

---

## 1. Highest-value: the dead planner generation

This is the biggest single win and it is **self-documented as removable**.

`backend/app/ai/agents/planner/planner_v2.py:1`:

> "Planner v2 — legacy JSON envelope output (DEPRECATED). Scheduled for
> deletion at v3 release. ... Once v3 has soaked in prod for one release,
> this file and `prompt_builder.py` will be removed."

Current state of that directory:

| File | LOC | Status |
|---|---|---|
| `planner.py` (class `Planner`, **v1**) | 763 | **Dead.** Only reachable via the `from .planner import Planner` re-export in `__init__.py`. Never instantiated anywhere in `app/`, `tests/`, `main.py`. |
| `prompt_builder.py` (v1/v2 builder) | 1,263 | Live only on the `BOW_PLANNER=v2` fallback path |
| `planner_v2.py` | 244 | Live only on `BOW_PLANNER=v2` |
| `planner_state.py` | 21 | Only imported by `planner_v2.py` |
| `prompt_blocks.py` | 32 | Exists to keep v2 and v3 prompt text in sync |

v3 is the default (`app/ai/agent_v2.py:1051`); v2 is reachable only by setting
`BOW_PLANNER=v2`. Retiring the flag deletes **~2,300 LOC**, removes the
if/else at `agent_v2.py:1052-1070`, and removes the dual-contract maintenance
burden that `prompt_blocks.py` exists to carry.

Two more agents fall out with it:

- `app/ai/agents/answer/answer.py` (164 LOC) — `AnswerAgent` is referenced
  **nowhere** in the repo. Dead.
- `app/ai/agents/dashboard_designer/dashboard_designer.py` (265 LOC) —
  `DashboardDesigner` is referenced only by its own `__init__.py`. Dead. Its
  org-setting gate is read only from the dead `planner.py:215,434`.

---

## 2. The `ai_features` settings block is a no-op surface

`backend/app/schemas/organization_settings_schema.py:395-404` exposes seven
AI toggles: `planner`, `coder`, `validator`, `dashboard_designer`,
`analyze_data`, `code_reviewer`, `search_context`.

Where each is actually read:

- `dashboard_designer` → only `app/ai/agents/planner/planner.py` (dead, §1)
- `code_reviewer`, `search_context` → only `app/ai/agents/answer/answer.py` (dead, §1)
- `planner`, `coder`, `validator`, `analyze_data` → **read nowhere**

The frontend already treats it as dead: `frontend/pages/settings/ai_settings.vue:325`
comments "`ai_features` is a legacy dict of…" and skips it at line 331. The only
remaining live consumer is a backwards-compat name lookup in
`app/ai/tools/mcp_tool_registry.py:60` (`mcp_native_tools_max` → `tools_max`),
which can be kept as a one-line special case.

So: a user-facing settings block where **four toggles do nothing at all** and
three gate dead code. This is worth removing both for the LOC and because it
mis-advertises behaviour to admins.

---

## 3. Confirmed-dead backend modules (safe deletes)

Unreachable from every entry point, and with no string/dotted reference
anywhere in `app/`, `tests/`, `scripts/`, `alembic/`, `tools/`, `main.py`:

| File | LOC |
|---|---|
| `app/core/derived_access.py` | 143 |
| `app/schemas/dbt_config_schema.py` | 235 |
| `app/schemas/completion_block_schema.py` | 69 |
| `app/schemas/llm_usage_schema.py` | 32 |
| `app/services/agent/agent_execution_service.py` | 64 |
| `app/services/agent/tool_execution_service.py` | 86 |
| `app/services/agent/plan_decision_service.py` | 43 |
| `app/models/application.py` | 14 |
| `app/schemas/application_schema.py` | 0 (empty file) |
| `app/ai/agent.py` | 1 (empty) |
| `app/ai/agents/router.py` | 0 (empty) |
| `app/services/git_repository_service.py` | 11 — a "Legacy compatibility module" re-exporting `GitService` that nothing imports |

Plus `class CodeExecutionManager` at `app/ai/code_execution/code_execution.py:1230`
— a "Deprecated shim" whose main method raises `RuntimeError`. Zero references
outside its own file (~25 LOC).

**§1 + §3 ≈ 3,100 LOC of pure delete, no behaviour change.**

---

## 4. Orphaned / half-built connectors

Connector clients are resolved by `resolve_client_class()` from the `REGISTRY`
key in `app/schemas/data_source_registry.py:553`. Three client modules have no
registry key and no caller:

| Client | LOC |
|---|---|
| `app/data_sources/clients/gcp_client.py` | 282 |
| `app/data_sources/clients/google_analytics_client.py` | 256 |
| `app/data_sources/clients/service_demo_client.py` | 116 |

`GoogleAnalyticsConfig` / `GoogleAnalyticsCredentials` are *imported* at
`data_source_registry.py:37,200` but never used in any entry — an unused import
that makes the connector look wired when it isn't.

Also orphaned, with no client and no registry entry:

- **ADP** — `ADPCredentials` + `ADPConfig` at `app/schemas/data_sources/configs.py:502,509`,
  plus `frontend/public/data_sources_icons/adp.png`. A connector stub abandoned
  mid-build.
- **ServiceDemo** — `ServiceDemoConfig`/`Credentials` at `configs.py:1017,1022`.

Adjacent (worth a glance, not necessarily a delete): `presto_client.py` (217 LOC)
has no registry key and is referenced only from
`tests/unit/test_engine_pool_identity.py`.

**Inverse finding (small bug, not cleanup):** 7 registry types have no icon file
— `businessobjects`, `custom_api`, `qlik_sense`, `qlik_sense_onprem`, `sap_bw`,
`sharepoint_onprem`, `gmail_mail` — so those render a broken image in the
connector picker.

---

## 5. Frontend dead code

**Components with zero references** (checked PascalCase, kebab-case, and the
Nuxt directory-prefixed auto-import name):

| File | LOC |
|---|---|
| `components/datasources/ResourcesSelector.vue` | 541 |
| `components/TrainingInstructionsSummary.vue` | 267 |
| `components/monitoring/ManageSuitesModal.vue` | 203 |
| `components/PublishModal.vue` | 131 |
| `ee/components/UpgradeBanner.vue` | 46 |

**Composables / utils with zero references:** `composables/useDebouncedRef.ts`
(20), `composables/usePublicAgent.ts` (24), `utils/agentIcon.ts` (52),
`utils/projectRoute.ts` (26).

**`pages/old_agents/**` — 11 routable pages, 4,178 LOC.** Still reachable:
`layouts/data.vue` links to it in five places (lines 7, 125, 138, 234-236), and
`components/KnowledgeExplorer.vue:3412` notes "was: window.open the legacy
/old_agents page". `frontend/tests/data_sources/list-data-sources.spec.ts:5`
calls it "the legacy data-sources list". This is the second-biggest frontend
win after the dashboard surface — but it needs the `layouts/data.vue` nav
decided first (that layout exists mostly to serve these pages).

**`pages/i18n-smoke.vue` (35 LOC)** is a test-only public page shipped in the
production route table; only `tests/i18n/locale-sweep.spec.ts` uses it. Either
gate it to non-production or move the assertion onto a real page.

**Nuxt 2 leftovers in `package.json`**: `@nuxt/types`, `@nuxt/typescript-build`,
`@nuxt/typescript-runtime` — all Nuxt 2 packages in a Nuxt 3.21 project, zero
references. Also `@types/diff-match-patch` and `http-proxy` are unreferenced.

---

## 6. Unused Python dependencies

Verified by grepping the actual import name, not the package name:

| Dep | Why it's dead |
|---|---|
| `vertica-python` | `vertica_client.py` imports `verticapy`, never `vertica_python` |
| `google-generativeai` | superseded by `google-genai`; only `from google import genai` is used (`app/ai/llm/clients/google_client.py`) — two Google SDKs pinned, one unused |
| `rapidfuzz` | no import anywhere |
| `xlsxwriter` | no import anywhere (`openpyxl` is what's used) |
| `ipython` | no `IPython` import anywhere |

Each drops install size and one more thing for Snyk/Aikido to flag.
(I did **not** flag `bcrypt`/`argon2-cffi` — they look like deliberate
transitive pins for `fastapi-users`' password helper.)

---

## 7. Repo weight: 203 MB of tracked media, 177 MB of it PR evidence

`media/pr/` holds **154 directories, 1,217 files, 177 MB** of per-PR
before/after screenshots and GIFs — the mandatory output of the `ui-evidence`
skill, committed permanently. Individual files run to 4.6 MB
(`media/pr/internal-artifact-verification/filter-detail-flow.gif`).
`docs/feedback-loops/` adds **21 MB across 344 files** of per-task reports;
`docs/screenshots/` 3.7 MB; `docs/design/` 6.2 MB.

Every clone pays for all of it forever. This is the largest cleanup by bytes by
an order of magnitude. Options, roughly in order of how disruptive they are:

1. Keep the skill, but have evidence live on the PR (GitHub-hosted upload) and
   stop committing `media/pr/**` going forward. Cheapest, fixes the growth rate.
2. Prune `media/pr/**` for already-merged PRs from the working tree (links in
   old `docs/feedback-loops/*.md` go stale — those are throwaway reports anyway).
3. Rewrite history / `git-lfs` migrate to reclaim the 177 MB from the object
   store. Only worth it if clone time is actually hurting; it rewrites every SHA.

Either way, `.agents/skills/ui-evidence/SKILL.md` is the place to change the
rule, otherwise this regrows at the observed ~150 PRs' worth.

---

## 8. One-off agent scripts: 211 files, 42 referenced by nothing

- `tools/agent/` — 141 files at the top level: 29 `verify_*`, 12 `seed_*`,
  8 `e2e_*`, 7 `repro_*`, 6 `shoot_*`, 5 `mock_*`, 4 `stub_*`…
- `backend/scripts/` — 70 files: 18 `seed_*`, 8 `bench_*`, 7 `verify_*`,
  4 `profile_*`, 2 `backfill_*`…

42 of these are referenced from **no** doc, skill, workflow, or other script —
including `backend/scripts/backfill_read_file_preview_projection.py` (a one-shot
backfill), `seed_users_memberships.py`, `sustained_load.py`,
`whatsapp_sandbox_debug.py`, `tools/agent/reset_backend.sh`. The rest are
referenced only from `docs/feedback-loops/*.md`, which are themselves
per-task throwaways.

They all still import valid modules (I checked every `app.*` import resolves),
so none are *broken* — they're just archaeology. Worth distinguishing the handful
that `AGENTS.md` actually blesses as shared setup (`tools/agent/boot_stack.sh`,
`seed_org.py`, `capture.mjs`) from the ~200 that are single-use.

Also stray: `backend/repro_dup_schedule.py` and `frontend/.repro/` (a
`repro.mjs` + `seed_repro.py` pair) — single-bug repro harnesses parked at the
root of each app.

---

## 9. Smaller, concrete items

**Confirmed-dead API endpoints** (the API surface is otherwise clean — only 3 of
603 endpoints are unreachable):
- `GET /api/console/recent-widgets` (`app/routes/console.py`)
- `GET /api/tests/metrics` (`app/routes/test.py`)
- `GET /api/users/avatar/{avatar_key}` (`app/routes/branding.py`) — referenced
  only in a migration comment
- `GET /api/reports/{report_id}/completions.legacy` (`app/routes/completion.py:213`)
  — explicitly named `.legacy`, no caller

**Stale deprecation that was never actioned:**
`app/ai/context/builders/resource_context_builder.py:2` says "DEPRECATED: This
module is deprecated and will be removed in a future version. Use
InstructionContextBuilder instead" — but it is still wired into
`context_hub.py:71,362` and `app/ai/tools/mcp/get_context.py:11,94`, 570 LOC.
Either finish the migration or drop the notice; right now it's a false signal.

**Misnamed, not dead:** `app/ai/tools/implementations/edit_artifact_legacy.py`
is 1,751 LOC and the *current* `edit_artifact.py` imports from it
(lines 144, 431), as does `tools/mcp/edit_artifact.py:237`. The "legacy" name
is actively misleading — it's the shared implementation. Rename rather than delete.

**Locale key drift** — `AGENTS.md` states all three catalogs must have identical
shape or the sync check fails. They don't: `locales/en.json` has 5,515 keys,
`es.json` is missing **31**, `he.json` is missing **11** (both missing the whole
`onboarding.llm.vertex*` / `projectId` group added with Vertex support; `es` also
missing `queries.codeLabel` and others). Either the sync check isn't running in
CI or it's not failing the build.

**Env-flag sprawl** — 129 distinct `BOW_*` flags read from `os.environ` in
`backend/app`, **64 of which appear in no doc, YAML, compose file, or chart**.
Beyond `BOW_PLANNER` (§1), these look like collapse candidates where the legacy
side is no longer wanted: `BOW_PLANNER_TRANSCRIPT` (`transcript_bridge.py:84`:
"On by default; `BOW_PLANNER_TRANSCRIPT=0` restores the legacy path"),
`BOW_SANDBOX_ZYGOTE`, `BOW_ARTIFACT_PRECOMPILE`, `BOW_FORCE_PARALLEL_TOOLS`,
`BOW_USE_TIKTOKEN`.

**No pytest config at all** — there is no `pytest.ini` and no
`[tool.pytest.ini_options]` in `backend/pyproject.toml`. The `-m ai` / `-m e2e`
markers and `--db` option come from conftest only, so markers are unregistered;
every CI invocation passes `--disable-warnings`, which hides that. 271 skip/xfail
marks across the suite — most are legitimate platform guards, but
`tests/integrations/ds_clients.py:36,40` permanently skip Snowflake and BigQuery
("CI account suspended (billing)", "dataset empty/permissions").

**Alembic** — 271 revisions, single head (healthy), but **36 of them are no-op
merge-head revisions** (24 literally named `*merge*`) with empty `upgrade()`.
That's the cost of many parallel branches. Since the git history here only goes
back to 2026-10-13, a squash-to-baseline is cheap and would cut the migration
directory by a third.

**Abandoned docs folders** — `documents/` (7 files, 64 KB) is superseded by
`docs/` + docs.bagofwords.com; 5 of its 7 files are referenced from nowhere, and
one is misspelled `langauge_models.md`. `audit/models/expectations.md` is a
single stray file in its own top-level directory.

**Duplicate skill definition** — `.claude/skills/sandbox-feedback-loop/SKILL.md`
(133 lines) and `.agents/skills/sandbox-feedback-loop/SKILL.md` (80 lines) are
**different content for the same skill**. `AGENTS.md` says skills live in
`.agents/skills/`, so the `.claude/` copy is a fork that will drift. Separately,
`.agents/skills/security-scan/` exists but is **not listed** in `AGENTS.md`'s
skill index.

---

## Suggested order

| # | Item | ~LOC / size | Risk |
|---|---|---|---|
| 1 | §3 dead modules + §1 dead agents (`planner.py`, `answer/`, `dashboard_designer/`) | ~3,100 | None — zero references |
| 2 | §5 dead frontend components/composables + §6 unused deps | ~1,300 + deps | None |
| 3 | §4 orphaned connectors (gcp, google_analytics, service_demo, ADP stub) | ~700 | None |
| 4 | §1 retire `BOW_PLANNER=v2` (`planner_v2`, `prompt_builder`, `planner_state`, `prompt_blocks`, the agent_v2 branch) | ~1,550 | Low — removes a fallback; needs a "v3 has soaked" call |
| 5 | §2 remove the `ai_features` settings block | small, but user-facing | Low — keep the `mcp_native_tools_max` compat lookup |
| 6 | §7 stop committing `media/pr/**`; amend `ui-evidence` SKILL | 177 MB | Process change |
| 7 | §8 prune the 42 unreferenced one-off scripts + the two `.repro` harnesses | 211 files | None |
| 8 | §5 retire `pages/old_agents/**` + `layouts/data.vue` | 4,589 | Medium — product decision + nav |
| 9 | §9 locale drift, pytest config, alembic squash, duplicate skill, `documents/` | small each | Low |

Items 1-3 are mechanical and could land as one PR each with no behaviour change.
Item 8 and the dashboard/widget work you already named are the two that need a
product call before any code moves.
