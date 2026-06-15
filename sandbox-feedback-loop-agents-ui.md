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

## 4. The loop

1. Edit `frontend/components/KnowledgeExplorer.vue`.
2. `nuxt dev` HMRs it — no restart.
3. Re-run the spec; eyeball the PNG; repeat.

Backend changes need a uvicorn restart (no `--reload` above).

## Notes / gotchas

- **Pending-review state** (suggestion cards, Discard button, Run-eval strip)
  only renders when an agent has a *pending instruction build*. A fresh admin
  signup has none, so that exact UI needs seeded data
  (`backend/scripts/seed_knowledge_demo.py` / `seed_access_demo.py`) or a
  non-admin edit that lands as a suggestion. The admin view still verifies the
  page, the create→left-refresh wiring, and the diff/version pane.
- Everything is **ephemeral** — sqlite DB, signup, and screenshots vanish when
  the sandbox is reclaimed. Commit screenshots worth keeping.
- No `typecheck`/`lint` script exists; `nuxt dev` booting + the spec passing is
  the practical compile check for a Vue SFC change.
