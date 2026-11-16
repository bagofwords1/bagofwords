# AGENTS Guidelines (repo root)

See `frontend/AGENTS.md` and `backend/AGENTS.md` for the per-side guides, and
`backend/tests/AGENTS.md` before writing or changing any backend test.

### Issues & PRs — read the authoring standard first

Before you **open a GitHub issue** or **write a PR description**, read the
matching standard **in full** and follow it. These are dual-audience artifacts
(a maintainer must be able to *decide* from the top; a future fixer/agent must
be able to *navigate and reproduce*), distilled from this repo's own issues/PRs:

- **`.claude/templates/issue_schema.md`** — how to open an issue: title
  `[Severity] area: precise outcome`, **user-impact first** (a named persona,
  what they see, why it hurts), a single **root cause with `file:line`**,
  **quoted evidence** (never "I think"), the **"tell"** that proves it's a real
  bug vs intended, honestly-**bounded scope** (what's NOT affected), an in-repo
  **correct-reference pattern**, a **minimal→fuller proposed fix**, and a
  code-derived **wireframe only for visual bugs**. Severity is a worded judgment,
  not a score.
- **`.claude/templates/PR_DESCRIPTION_STANDARD.md`** — the PR body shape:
  reviewer **decision box**, a product/customer **executive summary**, both a
  **product-impact and a technical visual**, an **evidence KPI**, and a
  **context map** — ordered so a reviewer can decide from the first screen and a
  future agent can navigate the change from the bottom.

### Agent skills

Reusable agent procedures live in `.agents/skills/<name>/SKILL.md`
(tool-agnostic location). Read and follow the matching skill before starting
one of these tasks:

- **sandbox-feedback-loop** — build a runnable reproduce→fix→verify loop for
  any bug/feature; reports land in `docs/feedback-loops/`.
- **qa** — map all user-facing functionality (`docs/qa/functionality-map.md`),
  then live-test flows against a running stack.
- **ui-audit** — sweep a page group control by control (every button, link,
  input) and role by role, recording expected-from-code vs actual-in-browser
  and filing the mismatches. Use for open-ended UI bug hunts and for verifying
  RBAC gating in the UI; `qa` covers flows, this covers the controls no flow
  touches.
- **ui-evidence** — capture the **mandatory** before/after screenshots (and
  GIFs/videos for flows) for any change touching
  `frontend/{pages,components,layouts,assets}/**`.
- **docs-update** — refresh docs.bagofwords.com (Mintlify MCP) with text +
  screenshots after user-facing changes ship.
- **localization** — the locale/i18n architecture end to end; follow it when
  adding strings, locales, or touching anything RTL.
- **add-connection-type** — add a data source/connector (client, config +
  credentials schemas, registry entry, tests) with verification steps.
- **add-llm-provider-or-model** — extend the LLM catalog; model id, pricing,
  and context window must be verified against official docs first.
- **release-notes** — bump `VERSION` and write a minimal, user-facing
  `CHANGELOG.md` entry after a user-facing change ships.
- **readme-showcase** — seed a polished product workspace, capture README
  screenshots, and refresh the product story around current capabilities.

Shared setup scripts (usable by humans too): `tools/agent/boot_stack.sh`
(run the full stack like CI), `tools/agent/seed_org.py` (seed org/users/demo
data through the real API), `tools/agent/capture.mjs` (screenshots/videos).
In a DevBox, read the next section first: it replaces most of their setup role.

### DevBox (`BOW_SANDBOX_DEVBOX=1`) — overrides every setup instruction in this repo

Check `echo $BOW_SANDBOX_DEVBOX` before following any boot, seed or environment step.
If it is `1` you are in a managed DevBox: the stack is already booted, migrated and
provisioned, and the rules below **win over** whatever a skill, design doc,
`docs/feedback-loops/*` doc, README or `tools/agent/*` header says. If it is unset or
`0`, ignore this section. Those docs were mostly written for fresh cloud sandboxes, so
read their setup steps through this table:

| The doc says | In a DevBox do this instead |
|---|---|
| `tools/agent/boot_stack.sh`, `restart_backend.sh`, `python main.py`, `uvicorn`, `yarn dev`, `kill`/`fuser -k`/`pkill` on the app | `bow-stack status` / `start` / `stop` / `restart [backend\|frontend]` (`bow-stack --help`). Never start or kill backend/frontend processes yourself. Backend runs uvicorn `--reload`, frontend `nuxt dev` with HMR: most edits need no restart. The frontend is never a production build here (run `yarn build` only to check the build, don't serve it). |
| `git checkout <ref>`, `pip install uv`, `uv sync --frozen --extra dev`, `yarn install`, `alembic upgrade head`, a separate venv | The venv and `node_modules` are ready: skip these. To change refs use `bow-stack switch <ref>` (re-syncs deps when lockfiles moved, migrates, restarts, re-provisions). Don't run the docs' `uv sync --extra dev`: the DevBox venv also carries the `kerberos` extra and uv would remove it. After editing dependencies yourself, sync the way the DevBox does — `(cd backend && uv sync --no-install-project --extra kerberos --extra dev)` / `(cd frontend && yarn install)` — then `bow-stack restart`. `bow-stack restart backend` runs migrations. |
| Create/delete the SQLite file, `reset_backend.sh`, "fresh database" | `bow-stack reset-db` (moves the old db aside, migrates, provisions). PostgreSQL: `devbox_use_postgres` / `bow-stack use-database`. |
| Register a user (`/users/sign-up`), create the org, `seed_org.py` (plain or `--demo`), `setup_haiku_llm.py` / `setup_openai_llm.py`, the onboarding LLM step, "Skip onboarding" | Already done at boot: admin + org, an Anthropic provider with Claude 4.5 Haiku as the default model, the demo data source, onboarding completed. Sign-up is closed once a user exists. Re-run provisioning with `bow-stack provision`. |
| Log in as `admin@example.com` / `Password123!` | The DevBox admin: `source tools/agent/devbox_env.sh` exports it as `BOW_ADMIN_EMAIL`/`BOW_ADMIN_PASSWORD` and `BOW_EMAIL`/`BOW_PASSWORD` (the names the agent scripts read). Files: `$BOW_WORKSPACE/data/admin.email`, `$BOW_WORKSPACE/data/admin.password`. Never print the password into docs, logs or PRs. |
| Extra fixtures (`seed_org.py --sqlite-sources N` / `--invite`, other `tools/agent/seed_*.py`, `setup_stub_llm.py`) | Fine to run, as the DevBox admin: `source tools/agent/devbox_env.sh` first (`seed_org.py` then logs in as that admin and finds the app database itself). |
| `backend/db/app.db`, `backend/db/agent.db`, `sqlite3 db/app.db` | The app database is `$BOW_DATABASE_URL` (`$BOW_SQLITE_DB` after sourcing `devbox_env.sh`; empty when the DevBox runs on PostgreSQL). It is persistent and yours: destructive audits are fine, then `bow-stack reset-db`. |
| `/tmp/bow-agent/*.log`, "the server log" | `bow-stack logs backend\|frontend`, or `$BOW_WORKSPACE/run/{backend,frontend}.log`. |
| Set/pin `BOW_ENCRYPTION_KEY`, `BOW_CHROMIUM_EXECUTABLE`, run `scripts/download-vendor-libs.sh` | Already done (key pinned per volume, vendor libs downloaded at boot and on `switch`). Don't override them. |
| `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`, `executablePath: '/opt/pw-browsers/chromium'`, `playwright install` | Browsers are pre-installed in Playwright's default location: set none of these and never run `playwright install`. |
| `pytest --db=postgres` / `--db=external` | `TEST_DATABASE_URL` is set to the **app database** in a DevBox, and both modes honour a pre-set value. Run them only with `TEST_DATABASE_URL` pointed at a throwaway database (e.g. a separate `devbox_postgres_deploy` instance; there is no Docker for testcontainers). The default `--db=sqlite` is safe as is. |
| `frontend/tests` Playwright suite (its `setup` project registers the first admin) | Assumes an empty database, which a DevBox never has; drive the UI with the DevBox admin's session instead. |

Everything else — the process in each skill, tests, verification, evidence, doc
templates — still applies. URLs inside the DevBox are the usual
`http://localhost:3000` (UI, proxies `/api`) and `http://localhost:8000` (backend).

### Locale / i18n (system overview)

Three locales ship today: `en` (default), `es`, `he`. Hebrew is the RTL
reference — anything RTL must work for `he`.

**Resolution order** (highest priority wins):

1. `X-Locale` request header — test-only override; must be in `enabled_locales`.
2. Per-user personal choice — lives in `localStorage.bow.locale` on the
   client. (A server-backed `users.locale` column is on the profile-page
   design roadmap but not yet built.)
3. Organization locale — stored in `OrganizationSettings.config["locale"]`,
   set via the picker in `/settings/general`.
4. System default — `bow_config.i18n.default_locale` (`en`).

**Catalogs** live at the repo root: `locales/en.json`, `locales/es.json`,
`locales/he.json`. All three must have identical shape — key drift fails
the sync check in `docs/design/i18n.md`. Hebrew vocabulary convention:
"הנחיות" (never "הוראות"), "גלריה" (never "לוח מחוונים"),
"בדיקות איכות" (never "הערכות").

**End-to-end flow on login:**

```
frontend/plugins/i18n.ts        registers createI18n, applies personal
                                 bow.locale if present, exposes $setLocale
frontend/layouts/default.vue    after session ready, fetches
                                 GET /api/organization/locale and applies
                                 effective_locale when no personal override
GET/PUT /api/organization/locale FastAPI routes; PUT requires manage_settings
backend/app/services/...         emails, LLM prompts, and typed errors each
                                 pull their own locale (see backend/AGENTS.md)
```

See `docs/design/i18n.md` for authoring rules, ESLint-plugin-vue-i18n
config, the catalog sync check, and RTL conventions.
