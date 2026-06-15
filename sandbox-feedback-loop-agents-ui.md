# Sandbox Feedback Loop — Agents / Knowledge Explorer UI (Playwright screenshots)

Brings the app up **inside an ephemeral sandbox** (no Docker) and drives it with
Playwright to verify the Knowledge Explorer changes — **Discard**, the
**Run-eval-with-progress** strip, and the **pending-review list refresh** — and
to capture screenshots.

Production runs one uvicorn process on **:3000** serving both the SPA and the API
(`SERVE_FRONTEND=1`). For a fast loop we split the halves:

```
nuxt dev (:3000)  ──/api, /ws/api proxy──▶  uvicorn (:8000)
        ▲                                         │
        └──────────── Playwright (chromium) ──────┘
```

Proxy targets live in `frontend/nuxt.config.ts` (`proxy.proxies`): `/api`,
`/ws/api`, `/mcp`, `/.well-known`, `/swagger` → `http://127.0.0.1:8000`.

## 0. One-time setup

```bash
cd frontend && npm install                      # node_modules usually present
npx playwright install chromium --with-deps     # lands in /opt/pw-browsers here
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

## 1. Backend on :8000 (sqlite)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export ENVIRONMENT=development
rm -f db/app.db                                 # fresh DB → first signup is org admin
alembic upgrade head                            # must resolve to a SINGLE head
uvicorn main:app --host 0.0.0.0 --port 8000 --log-level warning &
until curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/settings | grep -qE '200'; do sleep 2; done
```

## 2. Frontend dev server on :3000

```bash
cd frontend && npm run dev &                    # first boot ~30–90s
until curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/ | grep -qE '200'; do sleep 3; done
```

## 3. Playwright screenshots

Auth is handled by `tests/config/global.setup.ts` (signs up a fresh admin — the
first user becomes org admin — and saves `tests/config/admin.json`). The
`features` project already matches `tests/instructions/**`, so a spec dropped
there is logged in automatically.

```bash
cd frontend
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
export PLAYWRIGHT_BASE_URL=http://localhost:3000
npx playwright test tests/instructions/knowledge-explorer-shot.spec.ts --project=features
```

Spec used (`tests/instructions/knowledge-explorer-shot.spec.ts`): navigates to
`/instructions`, creates an instruction (exercising the **save → left-list
refresh** path), opens it, and screenshots the three-pane Knowledge Explorer.

## 3b. Pending-suggestion screenshots (no LLM)

The Discard / Run-eval / pending-review controls only render when an agent has a
**pending instruction build**. You don't need onboarding or an LLM for this — a
**member** editing an instruction produces a suggestion (it lands in
`pending_approval` because they're not an org admin). `seed_access_demo.py` does
exactly this against the live backend:

```bash
# 1. The seed expects admin sandbox@bow.dev to already exist AND own the org.
#    The FIRST uninvited signup auto-creates "Main Org" + admin role
#    (_ensure_org_for_first_uninvited_user), so register it first on a fresh DB:
curl -s -X POST http://localhost:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"sandbox@bow.dev","password":"Sandbox123!","name":"Sandbox Admin"}'

# 2. Seed agents (chinook.sqlite) + an editor member + many pending suggestions:
cd backend && BOW_SEED_BASE=http://localhost:8000 python scripts/seed_access_demo.py
```

Then capture the suggestion UI as the admin. The screenshot specs live in
`frontend/tests/instructions/*.shot.ts` and run via a screenshot-only config
(`frontend/pw.shot.config.ts`) that has **no** `globalSetup` (so it logs in as
the seeded admin instead of creating a fresh org-less one):

```bash
cd frontend
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers PLAYWRIGHT_BASE_URL=http://localhost:3000
npx playwright test tests/instructions/knowledge-explorer-suggestion.shot.ts --config=pw.shot.config.ts
# → screenshots/sugg-3-detail-suggested.png (Approve + Discard, no "View diff")
# → screenshots/sugg-4-diff-runeval.png      (diff header + Run-eval strip)
```

`*.shot.ts` files are intentionally NOT matched by the main `playwright.config.ts`
(which only runs `**/*.spec.ts`), so they never run in CI — they're manual
screenshot tools that depend on the seed above.

## 4. The loop

1. Edit `frontend/components/KnowledgeExplorer.vue`.
2. `nuxt dev` HMRs it — no restart.
3. Re-run the spec; eyeball the PNG; repeat.

Backend changes need a uvicorn restart (no `--reload` above).

## Notes / gotchas

- **Pending-review state** (suggestion cards, Discard button, Run-eval strip)
  only renders when an agent has a *pending instruction build* — see §3b for the
  no-LLM seed recipe that produces it.
- **First-user / org bootstrap**: only the FIRST uninvited signup auto-gets an
  org + admin role; later signups have no org until invited. The Playwright
  `globalSetup` creates a *new* admin each run, so repeated runs accumulate
  org-less admins — use a fresh DB + a single known admin for seeding.
- Everything is **ephemeral** — sqlite DB, signup, and screenshots vanish when
  the sandbox is reclaimed. Commit screenshots worth keeping.
- No `typecheck`/`lint` script exists; `nuxt dev` booting + the spec passing is
  the practical compile check for a Vue SFC change.
