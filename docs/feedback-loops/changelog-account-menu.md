# Feedback Loop — "add Changelog in the bottom-left Account menu, designed cleanly, and make sure the changelog file is reachable in the app container"

Add a **What's New** entry to the sidebar account menu that opens a clean,
timeline-styled release-notes view. The notes come from the repo-root
`CHANGELOG.md`, served by the backend at `/api/changelog`. The claim being
validated: the endpoint parses the real changelog, the menu opens the modal,
it renders correctly in light / dark / RTL, and the changelog file is present
inside the production Docker image (not silently dropped by `.dockerignore`).

## Root cause / design (validated)

- The account menu is a `UDropdown` built from `userDropdownItems` in
  `frontend/layouts/default.vue:768`. The "resources" group already holds
  Documentation / MCP / GitHub — the natural home for a **What's New** item.
- `CHANGELOG.md` (repo root, ~100 KB, 226 `## Version …` sections) is **not**
  copied into the backend image: the final Docker stage only copies
  `./backend`, and — critically — `.dockerignore` excludes `*.md` **and**
  `CHANGELOG*`, so a naive `COPY ./CHANGELOG.md` would fail the build. The
  backend runs from `/app/backend` (`start.sh:90`), so the repo root maps to
  `/app` (`config.py:10` reads `../VERSION` the same way).

## The change

- **Backend** `app/routes/changelog.py` — public `GET /api/changelog` parses
  `CHANGELOG.md` into `{ current_version, available, versions[] }`, where each
  version is `{ version, date, entries[] }` (entries are raw inline markdown).
  Path resolved via `CHANGELOG_PATH` env → `parents[3]/CHANGELOG.md`
  (`/app/CHANGELOG.md` in prod) → cwd → `/app/CHANGELOG.md`. Registered in
  `main.py`.
- **Docker** — `COPY ./CHANGELOG.md /app/CHANGELOG.md`, plus `!CHANGELOG.md`
  re-inclusion in `.dockerignore` (last matching pattern wins, so it overrides
  the `*.md` / `CHANGELOG*` excludes).
- **Frontend** — `components/ChangelogModal.vue` (timeline, "Latest" badge,
  markdown-rendered entries, light/dark/RTL) + a `changelog.*` i18n block in
  en/es/he + the menu item wired in `layouts/default.vue`.

## Loop A — deterministic parser test (no external services)

```bash
cd backend
export TESTING=true TEST_DATABASE_URL="sqlite:///db/agent.db" BOW_DATABASE_URL="sqlite:///db/agent.db"
uv run pytest tests/unit/test_changelog.py -q
```

Observed: `7 passed`. Covers heading order, date-present vs date-absent,
inline-markdown preservation, wrapped-line joining, pre-heading content
ignored, empty input, and version-with-no-entries.

## Loop B — live stack confirmation

```bash
tools/agent/boot_stack.sh --dev
curl -s http://localhost:8000/api/changelog | python3 -c \
  "import sys,json;d=json.load(sys.stdin);print(d['current_version'],d['available'],len(d['versions']))"
```

Observed: `0.0.433 True 226` — the endpoint parses every version from the real
changelog.

UI (Playwright, `frontend/media/pr/changelog-account-menu-sj06et/`): log in,
open the bottom-left account menu → **What's New** is present → the modal opens
and renders the timeline. Captured light, dark, and Hebrew (RTL).

## Bug found and fixed during verification

The **dark-mode** capture showed the bold entry titles as **invisible**
(near-black on a dark background). Cause: a Vue scoped-style rule authored as
`:global(.dark) .changelog-entry :deep(strong)` compiles to an invalid selector
and is silently dropped, so `strong` kept its light-mode `#111827`. Fixed by
authoring the override as `:global(.dark .changelog-entry strong)` — the proven
pattern used in `components/instructions/InstructionText.vue`. Re-captured:
titles render correctly in dark mode.

## What this proves / regression notes

- The parser test survives as a general regression (asserts the contract, not
  the one shipped changelog snapshot).
- The `.dockerignore` re-inclusion is the load-bearing fix for "reachable in
  the app container" — without it the image build breaks or the endpoint
  reports `available: false`.
