# OPS — Running the system & tests

How to run the app locally (Docker or from source), how to run every test suite,
and exactly what runs automatically when you open a PR. Everything below is
grounded in the real config files (`backend/pyproject.toml`,
`backend/tests/conftest.py`, `backend/tests/AGENTS.md`,
`frontend/playwright.config.ts`, `docker-compose*.yaml`, `Dockerfile`,
`.github/workflows/*`) — no invented commands.

> **Validation status (2026-07-09).** The backend unit command below was
> executed on this machine: the one-file smoke run passed (`tests/unit/test_clock.py`
> → 9 passed in 8s) and the full `tests/unit` run was re-run to confirm the
> summary (see "Backend tests"). Both `docker compose config` files **parse**
> (exit 0). The Docker **bring-up** (`docker compose up` / `docker build`) was
> **NOT** run end-to-end here because the Docker daemon was not reachable in this
> environment — the compose commands are transcribed from the real
> `docker-compose*.yaml` and README, not live-verified. Run them on a host with
> Docker running to confirm.

---

## TL;DR quickstart

```bash
# Run the app (released image, SQLite) — quickest look, NOT your local code:
docker run -p 3000:3000 bagofwords/bagofwords        # → http://localhost:3000

# Backend unit tests (pure logic, SQLite, no external services) — from repo root:
cd backend
uv sync --frozen --extra dev          # once, to build the .venv
uv run pytest tests/unit -q --disable-warnings
#  ↳ if `uv` PANICS (sandbox/agent shells), don't fight it — jump to
#    "⚠️ If `uv` fails or panics" under Backend tests: python3.12 -m venv .venv
#    && .venv/bin/python -m ensurepip --upgrade && .venv/bin/python -m pip install -e ".[dev]",
#    then use `.venv/bin/python -m pytest …` instead of `uv run pytest …`.

# Frontend E2E (Playwright) — needs the full app stack running first (see below):
cd frontend
yarn install --frozen-lockfile
npx playwright install --with-deps chromium
npx playwright test                   # against http://localhost:3000
```

The backend unit suite is self-contained (SQLite file DB, all externals mocked).
The Playwright suite is **not** self-contained — it drives a real browser against
a running frontend + backend + seeded admin user (details under "Frontend E2E").

---

## Test suites overview

| Suite | Location | Framework | Covers | Needs a running stack? |
|-------|----------|-----------|--------|------------------------|
| Backend **unit** | `backend/tests/unit/` (~60 files) | pytest | Pure logic, single service/helper, no HTTP | No — SQLite + mocks |
| Backend **e2e** | `backend/tests/e2e/` (~100 files, `@pytest.mark.e2e`) | pytest + `TestClient` | Full API flows: routes → services → real DB | No — in-process app + SQLite/Postgres |
| Backend **ai** | `backend/tests/ai/` (`@pytest.mark.ai`) | pytest | Agent/planner behavior | No, but needs `OPENAI_API_KEY_TEST` (real LLM) |
| Backend **evals** | `backend/tests/evals/` | pytest | LLM output-quality evals | Manual; real LLM |
| Backend **integrations** | `backend/tests/integrations/` | pytest | Real data-source / LLM provider creds | Manual / CI-gated; needs `integrations.json` |
| Frontend **E2E** | `frontend/tests/**/*.spec.ts` | Playwright | Browser flows: onboarding, members, reports, settings, RBAC… | **Yes** — running frontend + backend + DB |

There is **no frontend unit-test harness** — see the Playwright section for what
that means and how you'd add one.

---

## Run the system locally

Two fundamentally different paths — pick by what you're trying to do:

- **Docker Compose / `docker run`** → runs the **published** `bagofwords/bagofwords:latest`
  image. Fastest way to get a working app, but it runs the **released build, not
  your working-tree code.** Both `docker-compose.yaml` and `docker-compose.dev.yaml`
  declare `image: bagofwords/bagofwords:latest` with `pull_policy: always` — there
  is **no `build:` stanza**, so they never build this checkout. Do **not** use
  Compose to test this branch's changes (e.g. the #584 fix) — you'd be testing the
  last release.
- **From source** → runs **your** code. Either build the root `Dockerfile`, or run
  backend + frontend directly (the "Frontend E2E" bring-up below). Use this to
  exercise local changes.

### A. Docker — dev compose (no SSL): app + Postgres

`docker-compose.dev.yaml` brings up the app (`bagofwords/bagofwords:latest`) plus a
`postgres:16-alpine`, no reverse proxy. From the repo root:

```bash
# 1) Generate a Fernet encryption key (44 chars, ends with '='):
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2) Create .env at the repo root (the compose file reads these):
cat > .env <<'EOF'
POSTGRES_USER=bow
POSTGRES_PASSWORD=change_me_please
POSTGRES_DB=bagofwords
APP_PORT=3000
BOW_ENCRYPTION_KEY=<paste-the-key-from-step-1>
# BOW_LICENSE_KEY=            # optional, enterprise features
EOF

# 3) Bring it up (app on http://localhost:3000, Postgres on :5432):
docker compose -f docker-compose.dev.yaml up -d
docker compose -f docker-compose.dev.yaml logs -f app   # watch migrations + startup
# health check: curl -fsS http://localhost:3000/health

# 4) Tear down (add -v to also drop the DB/uploads volumes):
docker compose -f docker-compose.dev.yaml down
```

It mounts the repo-root `bow-config.yaml` read-only into the container and persists
`uploads/`, `branding/`, `logs/`, and Postgres data in named volumes. The app
container runs its own migrations on startup.

### B. Docker — production compose (Caddy + SSL)

`docker-compose.yaml` adds a `caddy:2.10-alpine` reverse proxy on 80/443 in front
of the same app + Postgres. It needs `DOMAIN=` in `.env` and a `Caddyfile`:

```bash
# .env needs DOMAIN=yourdomain.com plus the same POSTGRES_*/BOW_ENCRYPTION_KEY vars
docker compose up -d
```

### C. Single container (SQLite, no external DB) — from the README

```bash
docker run -p 3000:3000 bagofwords/bagofwords                       # SQLite (default)
docker run -p 3000:3000 \
  -e BOW_DATABASE_URL=postgresql://user:pw@host:5432/db \
  bagofwords/bagofwords                                             # your own Postgres
```

### D. Build & run YOUR local code with Docker

The root `Dockerfile` builds the app from this checkout (backend deps via
`uv sync --frozen`, frontend build, Playwright chromium, a Rust `qvd2parquet`
stage). Build context is the repo root:

```bash
docker build -t bagofwords-local .
docker run -p 3000:3000 \
  -e ENVIRONMENT=production \
  -e BOW_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  bagofwords-local
```

### E. From source, no Docker (best for iterating on this branch)

Run backend and frontend as processes — this is exactly the stack the Playwright
suite needs, so it's documented once under **Frontend E2E → "Prereqs — bring up
the stack"** below (backend on `:8000` via `uv run python main.py`, frontend on
`:3000`). This path runs your working-tree code and is what you want when
validating a change like #584.

> Compose paths A–C were **not** live-verified in the environment this doc was
> written in (no Docker daemon). They are transcribed verbatim from
> `docker-compose*.yaml` + README. The compose files themselves parse
> (`docker compose config` → exit 0).

---

## Backend tests (pytest)

### Toolchain & prereqs

- Python **3.12** (the project requires `>=3.12,<3.15`).
- **uv** is the package manager; deps and the lockfile live in
  `backend/pyproject.toml` + `backend/uv.lock`. The test deps are the
  `dev` optional-dependency group (`pytest`, `pytest-asyncio`,
  `pytest-timeout`, `testcontainers[postgres,mysql]`, `aiosmtpd`, `ruff`, `ty`).
- One-time setup: `cd backend && uv sync --frozen --extra dev` — this creates
  `backend/.venv` (Python 3.12) with everything the suite needs. `uv run …`
  then executes inside that venv. This is the same install CI uses.

> ### ⚠️ If `uv` fails or panics (restricted sandboxes / many agent environments) — READ THIS FIRST
>
> In some sandboxed environments (including some CI and AI-agent shells) `uv`
> **cannot run at all** — even `uv venv` or `uv sync` panics immediately, e.g.
> `thread 'main' panicked … Tokio executor failed` / a Rust `system-configuration`
> "Attempted to create a NULL object" crash (exit 101). This is `uv` failing to
> initialize, **not** a network or lockfile problem, and it happens *before any
> venv is created* — so any fallback that assumes `backend/.venv` already exists
> will not help. Recover with a plain venv + editable install (no `uv` at all):
>
> ```bash
> cd backend
> # 1) Build a Python 3.12 venv WITHOUT uv. Use a real 3.12 interpreter —
> #    `python3.12` (Homebrew/pyenv) is common; the system `python3` may be 3.9
> #    and will NOT work (project requires >=3.12).
> which python3.12 || echo "install Python 3.12 first (brew install python@3.12)"
> python3.12 -m venv .venv
> # 2) Bootstrap pip, then install BOTH the app runtime deps AND the dev/test
> #    tools in one step from pyproject.toml (the '.[dev]' extra). ensurepip is
> #    needed because a fresh venv here can lack pip.
> .venv/bin/python -m ensurepip --upgrade
> .venv/bin/python -m pip install -e ".[dev]"
> ```
>
> After this, use `.venv/bin/python -m pytest …` everywhere the doc says
> `uv run pytest …` (and `.venv/bin/python -m alembic …` / `.venv/bin/python main.py`
> for the from-source app in "Run the system locally"). Do **not** just install
> `pytest` alone — `tests/conftest.py` imports the whole app (sqlalchemy, fastapi,
> alembic, pydantic, …) at collection time, so the ~40+ runtime deps must be
> present too; `pip install -e ".[dev]"` provides both in one command.

### The database option (`--db`) — defined in conftest, not pyproject

`backend/pyproject.toml` has **no `[tool.pytest.ini_options]` section**. The
custom `--db` flag and the `e2e`/`evals` markers are registered in
`backend/tests/conftest.py` (`pytest_addoption` / `pytest_configure`). Choices:

- `--db=sqlite` — **default**, fast, no Docker. Uses a file DB under `backend/db/`.
- `--db=postgres` — spins up a throwaway `postgres:15` via **testcontainers**
  (needs Docker running). CI runs this leg too; your test must pass on both.
- `--db=external` — points at a pre-existing Postgres via `TEST_DATABASE_URL`
  (for sandboxes without Docker).

### Marker taxonomy

Markers are applied by **explicit decorators on the tests themselves**
(`@pytest.mark.e2e` in `tests/e2e/…`, `@pytest.mark.ai` in `tests/ai/…`) — not
auto-applied by directory. Consequences:

- `uv run pytest -m e2e` selects **only** the e2e-marked tests — it does **not**
  pick up `tests/unit/` (those files carry no marker).
- `uv run pytest tests/unit` selects the unit tests **by path** (they're plain,
  unmarked tests).
- `uv run pytest -m ai` selects the AI tests (the `ai` marker is used but not
  registered in `pytest_configure`, so pytest emits a harmless "unknown marker"
  warning — hidden by `--disable-warnings`).

### Commands (from `backend/`)

```bash
# Unit — fast, self-contained (recommended default while developing):
uv run pytest tests/unit -q --disable-warnings

# E2E on SQLite (what CI runs, sqlite leg):
uv run pytest -s -m e2e --db=sqlite --disable-warnings --timeout=600 --timeout-method=thread

# E2E on Postgres (needs Docker; CI runs this leg too):
uv run pytest -s -m e2e --db=postgres --disable-warnings --timeout=600 --timeout-method=thread

# AI tests (needs a real key):
OPENAI_API_KEY_TEST=sk-... uv run pytest -s -m ai --disable-warnings
```

Set `TESTING=true` when running anything by hand (per `tests/AGENTS.md`). CI also
sets `ENVIRONMENT=production` so the app loads the repo-root `bow-config.yaml`.

### What they depend on / mock

Per `tests/AGENTS.md`: **mock at boundaries only** — LLM providers, OAuth/identity
providers, external data-source drivers (ODBC/HTTP), SMTP, clocks. Everything
inside the app (services, models, DB) runs **real** against SQLite/Postgres —
that's what "e2e" means here. Seeding goes through `tests/fixtures/*`
(`create_user`, `login_user`, `create_organization`, …), which call the real
endpoints — not raw SQL.

### Runtime & the per-test migration fixture

`conftest.py` has an **autouse `run_migrations` fixture** that gives every test a
fresh schema:

- **SQLite**: the full alembic chain is replayed **once per session** into a
  `*.template` file; each test then **clones that template** (a millisecond
  filesystem copy) plus an engine-dispose. Fast per test, but it's paid ~890
  times.
- **Postgres/external**: the alembic chain is **replayed per test** (drops &
  recreates `public`), so that leg keeps the migration chain under CI coverage.

Measured on this machine: `tests/unit` = **890 passed, 17 failed, 9 skipped in
~12m23s** on SQLite. The 17 failures are pre-existing test-drift, unrelated to
any recent change. Budget ~12–15 min for the unit suite locally; the e2e suite is
much larger (CI allots **90 min**).

### Fully-explicit local command (proven in a restricted sandbox)

If `uv run` isn't usable (e.g. a sandbox where `uv pip install` can't build),
invoke the venv interpreter directly. This exact command was verified on this
machine:

```bash
cd backend
TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db" \
  .venv/bin/python -m pytest tests/unit \
  -q --disable-warnings --timeout=120 --timeout-method=thread
```

`BOW_DATABASE_URL` here only satisfies pydantic config **at import time** (there's
no `.env` in the tree); the conftest still overrides the actual test DB via
`TEST_DATABASE_URL`. See Troubleshooting for the env gotchas.

---

## Frontend E2E (Playwright)

`frontend/package.json` has **no `test` script and no vitest/jest** —
`@playwright/test` (pinned `1.55.1`) is the only test framework, a devDependency.
So the only automated frontend tests are the Playwright browser E2E specs.

### Configs and what each is for

| Config | Purpose | Run |
|--------|---------|-----|
| `playwright.config.ts` | **Main E2E suite.** `globalSetup` signs up the admin, then runs projects in order: `setup` → `onboarding` → (`members` seq. ∥ `features`) → `visibility`. `retries: 2`. | `npx playwright test` |
| `playwright.i18n.config.ts` | Locale sweep — **unauthenticated only**, no `globalSetup` (registration is one-shot). `tests/i18n/`. | `npx playwright test --config=playwright.i18n.config.ts` |
| `pw.empty.config.ts` | Screenshots of empty-state illustrations (`/evals`, `/scheduled-tasks`, `/queries`); no onboarding so pages stay empty. `tests/empty-states/*.shot.ts`. | `npx playwright test --config=pw.empty.config.ts` |
| `pw.shot.config.ts` | Minimal screenshot runner for any `*.shot.ts`. | `npx playwright test --config=pw.shot.config.ts` |

### Prereqs — you must bring up the stack first

The main config's `baseURL` is `http://localhost:3000` (override with
`PLAYWRIGHT_BASE_URL`). `globalSetup` (`tests/config/global.setup.ts`) navigates
to `/users/sign-up`, registers an admin (`TEST_ADMIN`), and saves the session to
`tests/config/admin.json` + `auth.json`. Specs then reuse that auth via the
`adminPage`/`memberPage` fixtures in `tests/fixtures/auth.ts`. So before running
you need: a **running frontend on :3000**, a **running backend on :8000** (the
frontend proxies `/api` to it), and a **fresh DB** (registration is disabled once
the first admin exists — a stale DB with an admin will make `globalSetup` fail).

Mirror CI's bring-up (from `.github/workflows/e2e-tests.yml`, `playwright-tests`
job):

> **`uv` note:** the backend commands below use `uv run …`. If `uv` panics in your
> environment, first do the from-scratch venv recovery in **"⚠️ If `uv` fails or
> panics"** (top of _Backend tests_), then replace `uv run alembic` →
> `.venv/bin/python -m alembic` and `uv run python main.py` → `.venv/bin/python main.py`.
> `BOW_ENCRYPTION_KEY` is **not required** to start the app from source with
> `ENVIRONMENT=production` (it loads `bow-config.yaml`); it's only needed for the
> Docker `.env` paths. (Verified: backend comes up healthy from source without it.)

```bash
# 1) Backend on :8000 with a fresh SQLite DB
cd backend
mkdir -p db uploads/files uploads/branding
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="sqlite:///db/playwright.db" \
  uv run alembic upgrade head                    # or: .venv/bin/python -m alembic upgrade head
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="sqlite:///db/playwright.db" \
  uv run python main.py &                         # or: .venv/bin/python main.py  — wait for http://localhost:8000/health

# 2) Frontend on :3000 (production build — routes precompiled, hydrates reliably)
cd ../frontend
yarn install --frozen-lockfile
NODE_OPTIONS="--max-old-space-size=4096" yarn build
node .output/server/index.mjs &                 # the @nuxt-alt/proxy in this server proxies /api → backend

# 3) Run the suite
npx playwright install --with-deps chromium
npx playwright test --workers=2
```

`yarn dev` also works for local iteration, but CI deliberately uses the
production build (`node .output/server/index.mjs`) because dev-mode on-demand Vite
compilation thrashed under parallel workers and timed out page loads.

### No frontend unit harness (honest gap)

There is **no** vitest/jest setup, so Vue component logic, composables, and store
logic are **not unit-tested today** — the only frontend coverage is end-to-end
through the browser. If you want fast, isolated frontend unit tests, add a
`vitest` devDependency + a `test` script in `frontend/package.json` and colocate
`*.spec.ts` next to the units; that's the natural place to grow it.

---

## Automatic testing on every PR (CI)

**One workflow gates PRs: `.github/workflows/e2e-tests.yml`** ("e2e tests"). Its
triggers:

```yaml
on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]
  workflow_dispatch:
```

So **every PR targeting `main`** runs it. Its jobs:

| Job | Runs on a PR? | What it does |
|-----|---------------|--------------|
| `e2e-tests` (matrix `db: [sqlite, postgres]`, 90-min cap) | ✅ always | `uv sync --frozen --extra dev`, then `uv run pytest -s -m e2e --db=<sqlite\|postgres>`. Also runs `-m ai` **only if `backend/app/ai/**` changed** (via `dorny/paths-filter`), with `OPENAI_API_KEY_TEST` from secrets. Env: `TESTING=true`, `ENVIRONMENT=production`. **⚠️ The always-run `-m e2e` set itself includes ~7 LLM tests (`test_llm_providers.py`, `test_completion.py`) that need `OPENAI_API_KEY_TEST` too — so this job goes RED on any fork PR (no secrets); see the fork-PR note below.** |
| `playwright-tests` (30-min cap) | ✅ always | Builds backend (`alembic upgrade head`, `python main.py`) on `sqlite:///db/playwright.db`, builds & serves the frontend production bundle, then `npx playwright test --workers=2`. Uploads `playwright-report` artifact on failure. |
| `integration-data-sources` | ⚠️ same-repo PRs only | `pytest -v tests/integrations/ds_clients.py`. Guarded by `github.ref == 'refs/heads/main' || (pull_request && head.repo == base.repo)` — **skips on fork PRs** (no secrets). Needs `INTEGRATIONS_JSON_B64`. |
| `integration-llms` | ⚠️ same-repo PRs only | Runs `tests/integrations/llm_clients.py` **only if `backend/app/ai/**` or `backend/app/models/llm*` changed**; skips google/bedrock/openai-reasoning cases. |
| `dispatch-build` | ❌ never on a PR | `if: github.ref == 'refs/heads/main'`; `needs: [e2e-tests, playwright-tests, integration-data-sources, integration-llms]`. On a green **main push**, dispatches the Docker build. |

Other workflows in `.github/workflows/` do **not** run on PRs: `integrations.yml`
and `ds-integrations.yml` are `workflow_dispatch`-only (manual integration runs);
`docker-image.yml` / `docker-image-branch.yml` / `release.yml` /
`helm-publish.yaml` / `helm-test.yml` are release/build plumbing.

> **Required-check note:** which of these jobs are *required to merge* is set in
> GitHub **branch-protection**, which is not stored in the repo — I can't confirm
> it from files. The workflow's own `dispatch-build.needs` shows the **intended**
> gates are `e2e-tests` + `playwright-tests` (+ the integration jobs on main).

### ⚠️ FORK / EXTERNAL PRs — `e2e-tests` will show RED, and it's (mostly) expected

If the PR comes from a **fork** (an external contributor without write access), the
CI behaves differently from a same-repo PR — don't be misled by the red:

1. **CI waits for approval.** GitHub holds fork-PR workflow runs at
   **`action_required`** until a maintainer clicks **"Approve and run"**. Until
   then, *no checks appear at all* (not a hang — an approval gate).
2. **`e2e-tests` goes RED even on a perfect PR.** GitHub does **not** pass repo
   **secrets** to `pull_request` runs from a fork (a security boundary — and this
   holds *even after* a maintainer approves the run). So
   `${{ secrets.OPENAI_API_KEY_TEST }}` resolves empty, and the LLM tests in the
   **always-run** `-m e2e` set — `backend/tests/e2e/test_llm_providers.py` and
   `test_completion.py` — **hard-fail** via `pytest.fail("OPENAI_API_KEY_TEST is
   not set")` (`backend/tests/fixtures/llm.py:138,194`; Anthropic at :99). Observed
   on a real fork PR: **`7 failed, 725 passed, 35 skipped`** on *both* db legs —
   all 7 are that missing-key error, none touch the change under test.
   (Note the inconsistency: the Azure branch of the same fixture uses
   `pytest.skip` when its key is absent — lines 12/15 — so it degrades gracefully;
   OpenAI/Anthropic use `pytest.fail`. Making those `skip` too would let fork PRs
   pass — tracked as a separate CI-robustness issue.)
3. **What still validates the change on a fork PR:** `playwright-tests` runs the
   full browser E2E and needs **no** LLM secret — so it *does* pass and is the
   meaningful signal for a frontend change. And the other **725** `-m e2e` tests
   pass; only the 7 secret-gated LLM tests fail.

**So on a fork PR:** a red `e2e-tests` whose only failures are
`OPENAI_API_KEY_TEST is not set` is **CI environment noise, not a real
regression**. Confirm by reading the failing job log (the 7 test names + that
exact message). A maintainer running it in a same-repo context (secrets present)
would see them pass. To characterize a fork-PR e2e failure, pull the job log:
`curl -sL -H "Authorization: token <tok>" .../actions/jobs/<job_id>/logs` and grep
for `FAILED` / `is not set`.

### KEY FINDING — `tests/unit` does NOT run in CI

**No CI job runs the ~60 backend unit-test files.** The `e2e-tests` job selects
`-m e2e`, which deselects everything in `tests/unit/` (those tests carry no
marker). There is no `pytest tests/unit` (or bare `pytest`) step anywhere in the
workflows. The unit suite is therefore only ever exercised when a developer runs
it locally — a PR can go green with a broken unit test.

If the goal is "tests run automatically for every PR" including unit tests, add a
job to `e2e-tests.yml` mirroring the existing style. Concrete, paste-ready:

```yaml
  unit-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
    - uses: actions/checkout@v7
    - name: Set up Python
      uses: actions/setup-python@v6
      with:
        python-version: '3.12'
    - name: Install dependencies
      working-directory: ./backend
      run: |
        pip install uv
        uv sync --frozen --extra dev
    - name: Run unit tests
      working-directory: ./backend
      env:
        TESTING: "true"
        ENVIRONMENT: "production"
      run: |
        uv run pytest tests/unit -m "not e2e and not ai" \
          --disable-warnings --timeout=120 --timeout-method=thread
```

(The unit suite is SQLite-only and self-contained, so it needs no `--db` matrix.)
If you want it to be a *required* check, add it to branch protection and to
`dispatch-build.needs`.

---

## Troubleshooting (symptom → fix)

- **`ModuleNotFoundError` / deps missing when you run `python -m pytest`.**
  The system `python3` (3.9 on this Mac) lacks the deps. Use the uv-managed venv:
  `cd backend && uv sync --frozen --extra dev`, then `uv run pytest …` — or call
  `backend/.venv/bin/python` (Python 3.12) directly.

- **`uv` panics / won't run at all (before any venv exists).** Even `uv venv` /
  `uv sync` crashes (`Tokio executor failed` / `system-configuration` NULL-object
  panic, exit 101). This is `uv` failing to initialize, not an install problem.
  → Use the no-`uv` from-scratch recovery in **"⚠️ If `uv` fails or panics"** at
  the top of _Backend tests_: `python3.12 -m venv .venv` →
  `.venv/bin/python -m ensurepip --upgrade` → `.venv/bin/python -m pip install -e ".[dev]"`.
  Then substitute `.venv/bin/python -m …` for every `uv run …` command in this doc.

- **`uv pip install` panics but a working `backend/.venv` ALREADY exists** (from a
  prior successful `uv sync`) and is only missing the test tools. *Precondition:
  the venv exists with the app runtime deps.* Then bootstrap just the test tools
  without uv: `backend/.venv/bin/python -m ensurepip` then
  `backend/.venv/bin/python -m pip install pytest pytest-asyncio pytest-timeout`.
  (If the venv does **not** exist, use the from-scratch recovery above instead —
  installing only pytest leaves `sqlalchemy`/`fastapi`/… missing and collection
  fails at `conftest.py` import.)

- **pydantic/config error at import (no `.env` in the tree).**
  The app needs a DB URL at import time. Either export
  `BOW_DATABASE_URL="sqlite:///db/app.db"`, or set `ENVIRONMENT=production` (like
  CI) so it loads the repo-root `bow-config.yaml`. The conftest still overrides
  the real test DB via `TEST_DATABASE_URL`.

- **Unit suite takes ~12–15 min.** Expected. The autouse `run_migrations`
  fixture rebuilds a fresh schema per test (SQLite clones a session template;
  Postgres replays migrations). It's ~890 tests × sub-second setup. Narrow with a
  path/`-k` filter while iterating: `uv run pytest tests/unit/test_clock.py`.

- **`-m e2e` "runs nothing" from `tests/unit`.** Correct — unit tests are
  unmarked; select them by path (`pytest tests/unit`), not by marker.

- **Playwright `globalSetup` fails at sign-up.** The DB already has an admin
  (registration is one-shot). Start from a fresh DB
  (`rm backend/db/playwright.db && alembic upgrade head`), and confirm backend
  `:8000/health` and frontend `:3000` are both up before running.

- **Postgres leg hangs / `--db=postgres` errors.** It needs Docker running
  (testcontainers). Without Docker, use `--db=sqlite` (default) or
  `--db=external` with a pre-set `TEST_DATABASE_URL`.

---

## Adding a new test

- **Backend unit** → `backend/tests/unit/test_<thing>.py`, plain unmarked test,
  seed via `tests/fixtures/*`. Run: `uv run pytest tests/unit/test_<thing>.py`.
- **Backend e2e** → `backend/tests/e2e/…/test_<flow>.py`, decorate with
  `@pytest.mark.e2e`, drive the real HTTP surface through `test_client`. Must pass
  on **both** `--db=sqlite` and `--db=postgres`.
- **Backend ai** → `backend/tests/ai/`, `@pytest.mark.ai`, needs
  `OPENAI_API_KEY_TEST`.
- **Frontend E2E** → `frontend/tests/<feature>/<name>.spec.ts`, import `test` +
  `expect` from `tests/fixtures/auth.ts` and use the `adminPage` / `memberPage`
  fixtures (they carry the seeded auth). Put it under a directory the main config
  routes into a project (`reports/`, `settings/`, `members/`, `visibility/`, …).

Follow `backend/tests/AGENTS.md`: test the **contract** (status codes, error
codes, invariants) not implementation details; cover **both admin and non-admin**
for anything permission-adjacent; every test must be able to **fail** (break the
code and watch it go red).

**Failure-path coverage (the #584 class — "silent delete failures").** This
codebase's recurring bug class is mutations that fail silently — a delete/update
that returns OK while the backend rejected it. Cover the *unhappy* path
explicitly, not just the success case:

- Backend e2e: assert the mutation's **error contract** for the forbidden/invalid
  case — e.g. a non-owner delete returns `403` (or the documented error code) and
  the row **still exists** afterward — not just that the happy-path delete
  returns 200.
- Frontend Playwright: force the API to fail and assert the **UI surfaces it**
  (a toast/error, no false "deleted" state). Intercept with `page.route`:

  ```ts
  await page.route('**/api/**/<resource>/*', route =>
    route.fulfill({ status: 403, body: JSON.stringify({ detail: 'forbidden' }) }));
  // trigger the delete, then assert an error is shown AND the item is still listed
  ```

  This is exactly the regression that a green happy-path-only suite lets through.
