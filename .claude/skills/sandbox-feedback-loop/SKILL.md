---
name: sandbox-feedback-loop
description: Boot a full local Bag of Words sandbox (backend + frontend + real LLM), drive it end-to-end through the UI with Playwright, and verify behavior at the DB / backend-log / HTTP layers. Use when a change needs real e2e validation (agent context, chat flows, file uploads, LLM behavior) rather than just unit tests.
---

# Sandbox feedback loop

Boot the app, drive it through the real UI, verify at every layer. Iterate.

## 1. Boot the sandbox

Backend (FastAPI, port 8000) — sqlite needs `BOW_DATABASE_URL`:

```bash
cd backend
uv sync --extra dev
mkdir -p db
BOW_DATABASE_URL='sqlite:///db/app.db' uv run alembic upgrade head
BOW_DATABASE_URL='sqlite:///db/app.db' uv run python main.py   # run in background, log to a file
# health: curl http://localhost:8000/health  (200; /docs is 404 in dev — not an error)
```

Frontend (Nuxt, port 3000):

```bash
cd frontend && yarn install && yarn dev   # background; ready when /users/sign-up returns 200
```

Caveats:
- The backend runs uvicorn with `--reload` watching the repo; running pytest in parallel creates `db/test_*.db` files that trigger restarts. Fine for a sandbox, but don't be surprised by restart noise in the log.
- In Claude Code remote env, TLS/proxy env vars (`SSL_CERT_FILE`, `HTTPS_PROXY`, `REQUESTS_CA_BUNDLE`) are pre-set and inherited — Anthropic API calls from the backend just work. The API key is in `$ANTHROPIC_KEY`.
- **Pin `BOW_ENCRYPTION_KEY`** (any Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) in the env you start the backend with. Unset, the key is invented per process — every restart/reload makes stored LLM-provider credentials undecryptable ("Agent failed:" with `Failed to decrypt stored payload` in the log) and invalidates every JWT (the auth secret is the same key), so Playwright `storageState` stops working.
- **Set `BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium`.** The backend's Playwright expects its own headless-shell download, which isn't in the container; without this the artifact render-validation and thumbnail steps fail silently (`BrowserType.launch: Executable doesn't exist`) and a syntactically broken artifact is persisted as "completed".
- **Run `bash scripts/download-vendor-libs.sh frontend/public/libs` once.** The artifact sandbox loads React/Babel/ECharts/Tailwind from `/libs/*.js`, which the Docker build downloads but a fresh checkout lacks — artifacts stay on "Loading..." with `React is not defined` in the iframe.
- Start the backend from `backend/` with an absolute-or-correct cwd (a relative `sqlite:///db/app.db` is resolved against the process cwd — a backgrounded start from the repo root silently creates a second, empty database).
- Never delete `db/app.db-wal`/`-shm` after a `kill -9`: the WAL holds un-checkpointed rows (users, providers, reports). Kill the whole process tree (uvicorn spawns `multiprocessing` helpers that keep the DB and port 8000 alive: `fuser -k 8000/tcp`), then restart — SQLite replays the WAL itself.
- The frontend's pinned Playwright (locale sweep, `playwright.i18n.config.ts`) looks for `chromium_headless_shell-<rev>/chrome-linux/headless_shell` under `/opt/pw-browsers`, which the container lacks; alias it to the installed browser instead of downloading: `mkdir -p /opt/pw-browsers/chromium_headless_shell-<rev>/chrome-linux && ln -sf /opt/pw-browsers/chromium /opt/pw-browsers/chromium_headless_shell-<rev>/chrome-linux/headless_shell` (the revision is in the error message).
- Uvicorn's `--reload` can hang at "Waiting for background tasks to complete" after the app has served chats; the new worker never starts and health goes to 000. Kill the process tree (`ps -eo pid,cmd | grep backend/.venv/bin/python`, then `fuser -k 8000/tcp`) and start again — do not touch the WAL.
- Nuxt dev pages compile on first hit and `networkidle` never settles (HMR socket); wait on a selector with a long timeout instead. The LLM settings page button is "Add Provider"; provider tiles are `img[alt="<provider> logo"]`; model checkboxes are identified by walking up to the text containing `Model ID:`.

## 2. Seed a user + org + LLM (one-time per fresh DB)

UI flow (Playwright): `/users/sign-up` has `#name`, `#email`, `#password` + `button[type=submit]`. Registration auto-logs-in and lands on `/onboarding` (org "Main Org" is auto-created for the first user).

**LLM setup — do it in `/settings/models`, not onboarding** (the onboarding LLM form is fiddly and marks the step done even if no models were saved):
1. All non-onboarding pages redirect to `/onboarding` until it's completed or dismissed; dismiss by clicking "Skip onboarding" on `/onboarding` (bottom of card) — after that settings pages render.
2. `/settings/models` → "Integrate Models" → "New Provider" → click `img[alt="anthropic logo"]` (provider tiles are images, no text).
3. Fill provider name (placeholder mentions "provider name") — must be unique **including soft-deleted providers** (409 otherwise; use e.g. `Anthropic-Haiku`).
4. Fill the API key input (the empty input with no placeholder) with `$ANTHROPIC_KEY`.
5. Model checkboxes carry no accessible label — resolve each checkbox's model by walking up ancestors until `innerText` contains `"Model ID:"`. For cheap tests keep ONLY "Claude 4.5 Haiku" checked (a single enabled model becomes both default and small-default automatically).
6. "Test Connection" should show "Successfully connected to LLM", then "Save Provider".
7. Verify in DB: `llm_models` has exactly the expected row with `is_enabled=1, is_default=1`.

## 3. Drive the chat UI

- The message input is `[contenteditable="true"]` (MentionInput), NOT a textarea.
- File attach: click the paperclip `button:has([class*="paper-clip"])` → a UModal opens with a hidden `input[type="file"]` (multiple) — `setInputFiles()` works on it. Wait for per-file check icons before closing.
- Close the modal by clicking the page background (`page.mouse.click(60,60)`), NOT Escape (Escape can eat the draft).
- Submit gating (`canSubmit`): needs non-empty text AND (a data source attached OR ≥1 uploaded file) AND no upload in flight AND a selected model. The draft (files + text) does NOT survive a page reload — do attach+type+send in one page session.
- Send = the last `button[class*="rounded-full"]`; Enter alone does not submit reliably.
- On send the URL becomes `/reports/{report_id}` — grab the id for DB checks. Wait for completion by polling for absence of `[data-testid="stop-button"]` and "Thinking" text.

Playwright setup (scratchpad, not the repo): `npm install playwright`, launch with `executablePath: '/opt/pw-browsers/chromium'`. Reuse login via `storageState`. Log every `/api/` response with `page.on('response')` into a jsonl file — that's your HTTP-layer verification. Screenshot every step; read screenshots to adapt selectors instead of guessing.

## 4. Agents (data sources) and their file libraries — API is fine for setup

Auth for direct API calls: the JWT is in the `auth.token` cookie (see Playwright `state.json`); send `Authorization: Bearer <jwt>` + `X-Organization-Id: <org id from GET /api/organizations>`.

- Create a files agent: `POST /api/data_sources` with `{name, type: "network_dir", config: {root_path: "<abs dir>"}, credentials: {auth_type: "none"}, auth_policy: "system_only"}`.
- Upload to its library: `POST /api/data_sources/{id}/files` (multipart field `file`).
- A NEW report created after this snapshots the agent's files into `report.files` (report_service).

## 5. Verify at the lower layers

DB (sqlite): `backend/db/app.db` — key tables: `completions` (role/status + `completion` JSON with content), `files`, `report_file_association` (has `completion_id` for turn attribution), `data_source_file_association`, `llm_models`, `reports`.

Backend log: `context_hub` INFO lines show prime_static/refresh_warm timings; `httpx` lines show real `POST https://api.anthropic.com/v1/messages` calls and status.

Context internals against real rows — import the app's full mapper registry via `import main`, then run any builder directly (run from `backend/` with `BOW_DATABASE_URL` set):

```python
import main  # registers all SQLAlchemy mappers; safe, uvicorn only runs under __main__
# ... create async sqlite session, load Report with selectinload(files, data_sources),
# run e.g. FilesContextBuilder(db, org, report).build() and inspect/render the section
```

This is the highest-signal check for context changes: it shows exactly what the planner would see for that report.

## 6. Iterate

Small numbered Playwright scripts (01_signup.js, 02_login.js, ...) beat one monolith: each failure is cheap to rerun, and `storageState` carries the session between them. When a selector fails: screenshot, read it, fix, rerun.
