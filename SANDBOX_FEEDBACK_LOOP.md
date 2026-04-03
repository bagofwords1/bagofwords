# Sandbox Feedback Loop Design

Design for enabling Claude to debug, iterate, and validate changes inside a sandbox environment.

## Principles

1. **Two feedback loops, not one.** Backend and frontend changes have different needs.
2. **Fixtures are the preferred action path.** Governed, token-managed, encode domain logic.
3. **curl is the escape hatch.** When no fixture exists, Claude reads route files and Pydantic schemas to construct the call.
4. **Persistent DB per session.** No per-test wipe. State accumulates like a real dev environment.
5. **Claude is the judge for visual output.** Playwright is a camera, not a test framework.

## Architecture Overview

```
                    SANDBOX SESSION
                    ===============

One-time setup:
  pip install -r requirements_versioned.txt
  cd frontend && yarn install && npx playwright install chromium
  cd backend && alembic upgrade head
  python main.py &          # auto-reload on .py changes
  cd frontend && yarn dev & # HMR on .vue/.ts changes
  python tests/sandbox_seed.py  # bootstrap user/org/data source

Two feedback loops:

  BACKEND CHANGE (.py)              FRONTEND CHANGE (.vue/.ts)
  ─────────────────────             ──────────────────────────
  pytest -s -m e2e --db=sqlite      Playwright screenshot
  Fixtures handle setup              Claude looks at the image
  Assertions validate                curl/fixtures drive the API
  Fast, deterministic                sqlite3 to inspect state
  No server needed (TestClient)      Servers already hot (HMR)
```

## Sandbox Setup

### Environment

No Docker. Bare Python 3.12 + Node 22 in the sandbox.

```bash
# Install backend deps
cd backend
pip install -r requirements_versioned.txt

# Install frontend deps
cd frontend
yarn install
npx playwright install chromium

# Create persistent SQLite DB
cd backend
mkdir -p db uploads/files uploads/branding
alembic upgrade head  # schema in db/sandbox.db
```

### Servers

Both run with auto-reload. Start once, leave running for the entire session.

```bash
# Backend — auto-reloads on .py changes (reload=True in main.py)
cd backend
TESTING=true python main.py &

# Frontend — HMR on .vue/.ts changes
cd frontend
yarn dev &

# Wait for both to be ready
timeout 60 bash -c 'until curl -s http://localhost:8000/health > /dev/null 2>&1; do sleep 1; done'
timeout 60 bash -c 'until curl -s http://localhost:3000 > /dev/null 2>&1; do sleep 1; done'
```

### Seed Script

Bootstraps the minimum state needed to use the app. Runs against the live server.

```python
# backend/tests/sandbox_seed.py
"""
One-time sandbox bootstrap. Run after servers are up.
Creates admin user, organization, and demo data source.
Saves session state to sandbox_state.json.
"""
import requests
import json

BASE = "http://localhost:8000"

def seed():
    # 1. Register admin user
    r = requests.post(f"{BASE}/api/auth/register", json={
        "email": "admin@sandbox.dev",
        "password": "Sandbox123!",
        "name": "Sandbox Admin"
    })
    assert r.status_code == 201, f"Register failed: {r.text}"

    # 2. Login
    r = requests.post(f"{BASE}/api/auth/jwt/login", data={
        "username": "admin@sandbox.dev",
        "password": "Sandbox123!"
    })
    token = r.json()["access_token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # 3. Create organization (auto-created on first user, fetch it)
    r = requests.get(f"{BASE}/api/organizations", headers=headers)
    org = r.json()[0]
    headers["X-Organization-Id"] = str(org["id"])

    # 4. Install demo data source (chinook SQLite)
    r = requests.get(f"{BASE}/api/demo-data-sources", headers=headers)
    demos = r.json()
    chinook = next(d for d in demos if "chinook" in d["name"].lower())
    r = requests.post(
        f"{BASE}/api/demo-data-sources/{chinook['id']}/install",
        headers=headers
    )
    ds = r.json()

    # 5. Save state
    state = {
        "token": token,
        "org_id": org["id"],
        "ds_id": ds["id"],
        "email": "admin@sandbox.dev",
        "password": "Sandbox123!",
        "base_url": BASE
    }
    with open("sandbox_state.json", "w") as f:
        json.dump(state, f, indent=2)

    print(f"Sandbox ready.")
    print(f"  Token:   {token[:20]}...")
    print(f"  Org ID:  {org['id']}")
    print(f"  DS ID:   {ds['id']}")
    print(f"  State:   sandbox_state.json")
    return state

if __name__ == "__main__":
    seed()
```

### DB Snapshots

The persistent SQLite DB can accumulate garbage over many iterations. Use snapshots to reset to a known good state.

```bash
# After seed, take a snapshot
cp backend/db/sandbox.db backend/db/sandbox_snapshot.db

# When state gets corrupted, restore
cp backend/db/sandbox_snapshot.db backend/db/sandbox.db
# Restart backend to pick up the fresh DB
```

## Feedback Loop 1: Backend (pytest)

For changes to `backend/app/**` files.

Uses the **existing pytest infrastructure** with no modifications. Fixtures handle setup, assertions handle validation, TestClient runs in-process (no server dependency).

```bash
# Run relevant e2e tests
cd backend
TESTING=true pytest -s -m e2e --db=sqlite -k test_relevant_thing --disable-warnings

# If changes touch backend/app/ai/**, also run AI tests
TESTING=true pytest -s -m ai --db=sqlite --disable-warnings
```

### How It Works

- `conftest.py` creates a fresh SQLite DB per test function (isolated from sandbox DB)
- Fixtures chain: `create_user` -> `login_user` -> `create_organization` -> `create_report` -> etc.
- TestClient calls endpoints in-process (no HTTP, no server)
- Each test gets clean state, asserts deterministic outcomes
- This is the same flow as CI (`e2e-tests.yml`)

### When to Use

- API logic changes
- Service layer changes
- Model/migration changes
- Auth/permission changes
- Any backend bug fix

## Feedback Loop 2: Frontend (Playwright + Claude's eyes)

For changes to `frontend/**` files or visual validation of artifacts.

Uses the **running servers** (python main.py + yarn dev) with the **seeded persistent DB**.

### Actions: How Claude Drives the App

**Priority 1 — Fixtures (governed, efficient):**

The existing test fixtures in `backend/tests/fixtures/` are the preferred way to perform actions. They encode domain logic, handle auth tokens, and know the exact payload shapes.

For the sandbox, fixtures are called via the real server (not TestClient). The inner functions are the same — they just need a different HTTP client. Example:

```python
# Fixture pattern (from tests/fixtures/report.py):
# response = test_client.post("/api/reports", json=payload, headers=headers)
#
# In sandbox, same call via requests:
# response = requests.post("http://localhost:8000/api/reports", json=payload, headers=headers)
```

Available fixture files (each contains multiple actions):

| Fixture file | Actions |
|---|---|
| `fixtures/user.py` | create_user |
| `fixtures/auth.py` | login_user, whoami |
| `fixtures/organization.py` | create_organization, add_member, update_member, remove_member |
| `fixtures/report.py` | create_report, get_reports, update_report, delete_report, publish, rerun, schedule, fork |
| `fixtures/completion.py` | create_completion, get_completions, create_completion_stream |
| `fixtures/data_source.py` | create_data_source, get_data_sources, test_connection, update, delete, get_schema, refresh_schema |
| `fixtures/instruction.py` | create_instruction, create_global_instruction, get/update/delete, bulk operations |
| `fixtures/eval.py` | create_test_suite, create_test_case, create_test_run, get results |
| `fixtures/file.py` | upload_file, upload_csv, upload_excel, get_files |
| `fixtures/connection.py` | create_connection, get/update/delete, test connectivity, refresh schema |
| `fixtures/api_key.py` | create_api_key, list, delete |
| `fixtures/build.py` | get_builds, get_build, get_main_build, get_diff, rollback |
| `fixtures/organization_settings.py` | get/update settings, upload/delete icon |
| `fixtures/console_metrics.py` | get_console_metrics, timeseries, table_usage, top_users |
| `fixtures/git_repository.py` | create/get/update/delete repo, index, sync, push |

**Priority 2 — curl (fallback, self-service):**

When no fixture exists for an action, Claude reads the source of truth and constructs a curl call:

1. **Route file** (`backend/app/routes/*.py`) — Shows endpoint path, HTTP method, auth dependencies, query parameters
2. **Pydantic schema** (`backend/app/schemas/*.py`) — Shows exact request body shape with field types, defaults, and validation

Route files to reference:

| Route file | Endpoints |
|---|---|
| `routes/report.py` | `/api/reports` CRUD |
| `routes/completion.py` | `/api/completions` (chat/stream) |
| `routes/artifact.py` | `/api/artifacts` CRUD, export, previews |
| `routes/data_source.py` | `/api/data-sources` CRUD, schema, metadata |
| `routes/instruction.py` | `/api/instructions` CRUD, bulk ops |
| `routes/llm.py` | `/api/llm` provider management |
| `routes/organization.py` | `/api/organizations` CRUD, members |
| `routes/auth.py` | `/api/auth` register, login, JWT |
| `routes/query.py` | `/api/queries` CRUD |
| `routes/step.py` | `/api/steps` execution steps |
| `routes/test.py` | `/api/test-suites`, `/api/test-runs` evals |

**Auth pattern** (same for all endpoints):
```bash
curl -X POST http://localhost:8000/api/{endpoint} \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID" \
  -H "Content-Type: application/json" \
  -d '{ ... payload matching Pydantic schema ... }'
```

### Visual Validation

For frontend changes and artifact output, Playwright takes screenshots and Claude evaluates visually.

```bash
# Take a screenshot of a specific page
cd frontend
npx playwright test tests/{relevant_dir}/ --workers=1

# Or use Playwright directly for a quick screenshot:
# (Claude can write a small script)
python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:3000/reports/{report_id}')
    page.wait_for_load_state('networkidle')
    page.screenshot(path='/tmp/screenshot.png', full_page=True)
    browser.close()
"
# Claude then reads /tmp/screenshot.png to evaluate the result
```

### State Inspection

Claude can query the persistent SQLite DB directly to understand what was persisted:

```bash
sqlite3 backend/db/sandbox.db "SELECT id, title, status FROM reports"
sqlite3 backend/db/sandbox.db "SELECT id, mode, version FROM artifacts WHERE report_id = '...'"
sqlite3 backend/db/sandbox.db ".schema artifacts"
```

### When to Use

- Vue/TypeScript component changes
- Artifact generation/rendering changes
- CSS/layout changes
- Any visual/UX change
- Flows involving SSE, WebSocket, or real HTTP middleware

## Artifact-Specific Flow

The artifact creation flow is the primary use case for visual validation. The generated UI is non-deterministic (LLM-generated React/HTML), so traditional assertions can't validate it.

```
Claude edits artifact generation code (e.g. create_artifact.py, prompts)
  |
  v
Trigger artifact creation:
  curl -X POST localhost:8000/api/completions \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Organization-Id: $ORG_ID" \
    -d '{"report_id": "...", "message": "Create a revenue dashboard"}'
  |
  v
Wait for completion, then screenshot:
  Playwright navigates to localhost:3000/reports/{report_id}
  Takes screenshot of the rendered artifact
  |
  v
Claude looks at the screenshot:
  - Does the dashboard show data?
  - Are charts rendered correctly?
  - Any JS errors visible?
  - Does the layout match the intent?
  |
  v
If not right: edit code -> servers hot-reload -> re-trigger -> re-screenshot
```

This mirrors the existing validation loop in `create_artifact.py` (lines 341-430) where Playwright renders artifacts and the LLM evaluates screenshots, but now applied to the development workflow itself.

## Environment Variables

Sandbox uses a minimal set of env vars. Defined in `.env.sandbox`:

```bash
# Required
TESTING=true
ENVIRONMENT=development

# Database (SQLite, no Postgres needed)
# Uses default from settings — no override needed

# Encryption (auto-generated if not set, per start.sh)
# BOW_ENCRYPTION_KEY=

# Optional: LLM for artifact generation / AI tests
# OPENAI_API_KEY_TEST=sk-...
```

## Decision Matrix: Which Loop to Use

| Files changed | Feedback loop | Command |
|---|---|---|
| `backend/app/**` (not ai/) | pytest e2e | `pytest -s -m e2e --db=sqlite` |
| `backend/app/ai/**` | pytest e2e + ai | `pytest -s -m e2e -s -m ai --db=sqlite` |
| `frontend/**` | Playwright | Screenshots + Claude evaluates |
| `backend/` + `frontend/` | Both | pytest first, then Playwright |
| Artifact prompts/tools | Artifact flow | Create artifact -> screenshot -> evaluate |

## What NOT to Change

- **Existing pytest fixtures** — Stay as-is for CI/CD. Per-test DB isolation is correct for deterministic testing.
- **CI/CD workflow** (`e2e-tests.yml`) — Unchanged. The sandbox is a separate, complementary workflow.
- **Fixture structure** — No dual-use refactoring. Fixtures remain pytest-native. Claude reads them as documentation and translates to curl when needed.

## Summary

```
CI/CD (existing, unchanged)           SANDBOX (new)
========================               =======

pytest + fixtures + assertions         Servers + seed + fixtures/curl + Claude's eyes
Fresh DB per test                      Persistent DB per session
TestClient (in-process)                Real HTTP (SSE, WS, middleware)
Deterministic pass/fail                Claude judges visual output
Runs on push/PR                        Runs during development

Shared source of truth:
  - Route files (backend/app/routes/*.py)
  - Pydantic schemas (backend/app/schemas/*.py)
  - Test fixtures (backend/tests/fixtures/*.py)
```
