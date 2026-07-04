---
name: qa
description: Run a live QA pass over the app — first map all user-facing functionality, then boot the full stack and manually exercise flows with Playwright, recording pass/fail evidence and filing a QA report. Use for release QA, post-merge smoke passes, or "QA the app / this area" requests.
---

# QA — map functionality, then test it live

Two phases, always in this order. The map is what keeps the live pass honest:
without it you test what's easy, not what exists.

## Phase 1 — Functionality map

Build (or refresh) `docs/qa/functionality-map.md` by enumerating from code, not
memory:

1. **Screens**: every route under `frontend/pages/**` (file-based routing).
2. **API surface**: routers in `backend/app/routes/*.py` — group by domain.
3. **Guards**: `frontend/middleware/{auth,permissions,onboarding}.global.ts` —
   note which flows require auth, permissions, or onboarding state.
4. **Existing coverage**: Playwright specs in `frontend/tests/**` and backend
   e2e in `backend/tests/e2e/**` — mark what's already automated.

Format — one row per user-facing flow:

| Area | Flow | Route(s) | API | Automated coverage | Last QA | Status |
|------|------|----------|-----|--------------------|---------|--------|

Keep rows behavioral ("invite a member and they can sign in"), not structural
("members page renders"). If a functionality map already exists, diff it
against the current routes/pages and update it rather than regenerating.

## Phase 2 — Live manual pass

1. **Boot the stack** (mirrors CI):
   ```bash
   tools/agent/boot_stack.sh                 # backend :8000 + built frontend :3000
   cd backend && uv run python ../tools/agent/seed_org.py --demo --invite member@example.com
   ```
2. **Pick scope**: full map for release QA; affected areas + their neighbors
   for a change-scoped pass. Say explicitly in the report what was NOT covered.
3. **Drive each flow with Playwright** like a careful human — through the UI at
   `http://localhost:3000`, not by calling APIs directly. Use throwaway specs or
   `cd frontend && node ../tools/agent/capture.mjs <url> <shot.png>` for
   evidence. `export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` in sandboxes.
4. **For every flow record**: PASS/FAIL, a screenshot, and for failures the
   exact repro steps + backend log excerpt (`/tmp/bow-agent/backend.log`).
5. **Verify both roles** where relevant — admin AND invited member (the seed
   script creates both). Permission gaps are a recurring bug class here.
6. **File the report** at `docs/feedback-loops/qa-<date>-<scope>.md`:
   scope, environment, table of flows with verdicts + evidence, and a
   findings section where each failure follows the sandbox-feedback-loop
   format (symptom → repro → suspected root cause with file:line).

## Rules

- A flow only counts as tested if you **observed the outcome state** (row
  appeared, email row created, chart rendered) — not just "no error thrown".
- Findings are findings: report them, don't silently fix mid-QA. Fixes go
  through the normal flow (sandbox-feedback-loop skill) after the pass.
- Update the functionality map's `Last QA` / `Status` columns as you go — the
  map is the durable artifact, the report is the snapshot.
- Never test against a production/staging deployment or real customer data.
