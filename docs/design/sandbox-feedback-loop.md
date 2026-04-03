# Sandbox Feedback Loop Design

How Claude debugs, iterates, and validates changes in a local dev environment.

## How It Works

Local dev is already running (`python main.py` + `yarn dev`). The DB is `backend/db/app.db` (persistent SQLite). Claude uses curl to drive the app, pytest for backend validation, Playwright for visual validation, and sqlite3 to inspect state.

```
Claude edits code
  → backend auto-reloads (python main.py has reload=True)
  → frontend HMR picks up .vue/.ts changes (yarn dev)
  → Claude validates:
      Backend change?  → pytest
      Frontend change? → Playwright screenshot → Claude looks at it
      Either?          → sqlite3 db/app.db to inspect state
      Not sure?        → curl the API and check the response
```

## Environment Setup

### Prerequisites

Requires Python 3.12+ (the codebase uses PEP 701 f-string syntax), Node 22, and yarn.

### System Dependencies

```bash
# Needed for psycopg2 build
apt-get install -y libpq-dev

# Upgrade setuptools (needed for thrift transitive dep)
pip install --upgrade setuptools
```

### Backend

```bash
cd backend

# Use Python 3.12 venv (system python may be 3.11)
python3.12 -m venv .venv
source .venv/bin/activate

# Install deps
pip install -r requirements_versioned.txt

# Required env vars (dev config references these)
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_SMTP_PASSWORD="dummy"

# Create directories and run migrations
mkdir -p db uploads/files uploads/branding
alembic upgrade head

# Start server (auto-reloads on .py changes)
python main.py &
```

### Frontend

```bash
cd frontend
yarn install

# Start dev server (HMR on .vue/.ts changes)
yarn dev &
```

### Playwright (for frontend screenshots)

```bash
cd frontend
npx playwright install --with-deps chromium
```

Note: The dev config is loaded from `configs/bow-config.dev.yaml` (not the root `bow-config.yaml`).

## Session State: `backend/sandbox_state.json`

All session data (credentials, tokens, IDs) is stored in `backend/sandbox_state.json`. This file is **gitignored** — it never gets committed. Claude reads it at session start and updates it after setup/login.

```json
{
  "credentials": {
    "email": "sandbox@bow.dev",
    "password": "Sandbox123!"
  },
  "session": {
    "token": "eyJ...",
    "org_id": "...",
    "ds_id": "..."
  },
  "endpoints": {
    "backend": "http://localhost:8000",
    "frontend": "http://localhost:3000"
  },
  "db_path": "backend/db/app.db"
}
```

## Sandbox Setup

### First-Time Setup

If the sandbox user doesn't exist yet (or `sandbox_state.json` has null session values):

```bash
# 1. Register sandbox user
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "sandbox@bow.dev", "password": "Sandbox123!", "name": "Sandbox Admin"}'

# 2. Login — save the token
curl -X POST http://localhost:8000/api/auth/jwt/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'username=sandbox@bow.dev&password=Sandbox123!'
# → {"access_token": "eyJ...", "token_type": "bearer"}

# 3. Get organization (auto-created on first user)
curl http://localhost:8000/api/organizations \
  -H "Authorization: Bearer $TOKEN"
# → [{"id": "...", "name": "..."}]  — save the org id

# 4. Install demo data source (chinook SQLite)
curl http://localhost:8000/api/data_sources/demos \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"
# → list of demos — find chinook, save its id

curl -X POST http://localhost:8000/api/data_sources/demos/$DEMO_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"
# → {"id": "...", "data_source_id": "..."}  — save data source id

# 5. Update sandbox_state.json with token, org_id, ds_id
```

### Returning Session

If `sandbox_state.json` already has session values, just re-login to refresh the token:

```bash
curl -X POST http://localhost:8000/api/auth/jwt/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'username=sandbox@bow.dev&password=Sandbox123!'
# Update token in sandbox_state.json
```

## Auth Pattern

All API calls use the same auth headers:

```bash
curl -X {METHOD} http://localhost:8000/api/{endpoint} \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID" \
  -H "Content-Type: application/json" \
  -d '{ ... }'
```

## Popular Commands

### Reports

```bash
# Create report
curl -X POST http://localhost:8000/api/reports \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID" \
  -H "Content-Type: application/json" \
  -d '{"title": "Test Report", "data_sources": ["$DS_ID"]}'

# List reports
curl http://localhost:8000/api/reports \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"

# Get report
curl http://localhost:8000/api/reports/$REPORT_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"

# Delete report
curl -X DELETE http://localhost:8000/api/reports/$REPORT_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"
```

### Completions (Chat)

```bash
# Create completion (triggers AI agent)
curl -X POST http://localhost:8000/api/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID" \
  -H "Content-Type: application/json" \
  -d '{"report_id": "$REPORT_ID", "message": "Show revenue by month"}'

# List completions for a report
curl http://localhost:8000/api/completions?report_id=$REPORT_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"
```

### Data Sources

```bash
# List data sources
curl http://localhost:8000/api/data-sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"

# Get schema for a data source
curl http://localhost:8000/api/data-sources/$DS_ID/schema \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"
```

### Instructions

```bash
# Create instruction
curl -X POST http://localhost:8000/api/instructions \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID" \
  -H "Content-Type: application/json" \
  -d '{"content": "Always use metric units", "scope": "global"}'

# List instructions
curl http://localhost:8000/api/instructions \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"
```

### Artifacts

```bash
# Get artifacts for a report
curl http://localhost:8000/api/artifacts/report/$REPORT_ID \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"

# Get latest artifact
curl http://localhost:8000/api/artifacts/report/$REPORT_ID/latest \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Organization-Id: $ORG_ID"
```

## When curl Isn't Enough

If the endpoint isn't listed above, read the source of truth:

1. **Route file** (`backend/app/routes/*.py`) — endpoint path, method, auth, query params
2. **Pydantic schema** (`backend/app/schemas/*.py`) — exact request body shape, field types, defaults

Key route files:

| File | What |
|---|---|
| `routes/report.py` | Reports CRUD |
| `routes/completion.py` | Chat completions, streaming |
| `routes/artifact.py` | Artifacts CRUD, export, previews |
| `routes/data_source.py` | Data sources CRUD, schema, metadata |
| `routes/instruction.py` | Instructions CRUD, bulk ops |
| `routes/llm.py` | LLM provider management |
| `routes/organization.py` | Organizations, members |
| `routes/query.py` | Saved queries |
| `routes/test.py` | Eval suites and runs |
| `routes/connection.py` | External connections |
| `routes/step.py` | Execution steps |
| `routes/build.py` | Build versioning |

## Backend Validation: pytest

For backend code changes. Uses existing test infrastructure — no modifications needed.

```bash
cd backend

# Run e2e tests
TESTING=true pytest -s -m e2e --db=sqlite --disable-warnings

# Run specific test
TESTING=true pytest -s -m e2e --db=sqlite -k test_create_report --disable-warnings

# Run AI tests (when changing backend/app/ai/**)
TESTING=true pytest -s -m ai --db=sqlite --disable-warnings
```

Pytest uses its own isolated DB (not `app.db`). Each test gets a fresh DB via fixtures. This matches CI exactly.

## Frontend Validation: Playwright + Claude's Eyes

For frontend/visual changes. Servers must be running.

### Run Existing Playwright Tests

```bash
cd frontend

# Run all tests
npx playwright test --workers=2

# Run specific test suite
npx playwright test tests/reports/ --workers=1

# Run a single test file
npx playwright test tests/reports/create-report.spec.ts
```

### Quick Screenshot (Ad-Hoc)

```python
# Take a screenshot for Claude to evaluate
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:3000/reports/{report_id}')
    page.wait_for_load_state('networkidle')
    page.screenshot(path='/tmp/screenshot.png', full_page=True)
    browser.close()
```

Claude reads the screenshot to evaluate: Does it look right? Any errors? Data showing?

### Artifact Visual Validation

For LLM-generated UI (non-deterministic — can't assert, must look):

```
1. Create a completion that generates an artifact (via curl)
2. Playwright screenshots the rendered artifact
3. Claude looks at the screenshot
4. If wrong → edit code → servers hot-reload → re-trigger → re-screenshot
```

## State Inspection: SQLite

```bash
# If sqlite3 CLI is available:
sqlite3 backend/db/app.db "SELECT id, title, status FROM reports"
sqlite3 backend/db/app.db ".tables"

# If not (common in sandboxes), use Python:
python -c "
import sqlite3
conn = sqlite3.connect('backend/db/app.db')
for row in conn.execute('SELECT id, title, status FROM reports'):
    print(row)
conn.close()
"
```

## Decision Matrix

| Change | Validate with |
|---|---|
| Backend API/service/model | `pytest -s -m e2e --db=sqlite` |
| Backend AI logic | `pytest -s -m ai --db=sqlite` |
| Frontend component/page | Playwright screenshot → Claude evaluates |
| Artifact generation | curl → trigger creation → screenshot → Claude evaluates |
| Not sure what broke | `sqlite3 app.db` to inspect + server logs |

## What Stays Unchanged

- **Existing pytest fixtures** — for CI/CD, per-test isolation, deterministic
- **CI/CD workflows** (`e2e-tests.yml`) — sandbox is complementary, not a replacement
- **`backend/db/app.db`** — persistent, not wiped between iterations
