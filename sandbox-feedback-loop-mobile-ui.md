# Sandbox Feedback Loop — Mobile UI sweep (artifacts, chat, navigation, inputs)

Reproduces and validates a set of mobile‑web UI problems reported from an
iPhone/Android browser, and the fixes for them. The reported symptoms:

1. **Public artifact top bar looks broken on mobile** — the "Back to app",
   tab, "Refreshed …", "Edit Report"/"Fork" and close (✕) controls overlap
   each other into an unreadable pile.
2. **Vertical scroll / stray spacing** on the artifact and chat views, and the
   prompt box on the report page looks loose and is partly clipped.
3. **The artifact/report browser tab (and "Add to Home screen" shortcut) shows
   the report UUID** instead of the report name.
4. **Tapping any input zooms and scroll‑jumps the page** (seen on the login
   screen, but it happens app‑wide).

A fifth issue surfaced during the sweep:

5. **The whole app has no navigation on mobile** — the sidebar is translated
   off‑canvas below `sm:` and there was no hamburger to bring it back, so
   Home / Reports / Dashboards / Settings were unreachable on a phone.

---

## Root causes (validated)

| # | Symptom | Root cause |
|---|---------|-----------|
| 1 | Top bar overlap | `frontend/pages/r/[id]/index.vue` top bar hard‑codes the center block to `px-[200px]` and positions "Back to app" / action cluster with `absolute start-4` / `end-4`. On a ~390px screen the 400px of padding collapses the center and the absolute items overlap the tabs. Desktop‑only layout, no `sm:` breakpoint. |
| 2 | Scroll / loose prompt box | Full‑height shells use `h-screen` (`100vh`), which on mobile is taller than the visible viewport once browser chrome is counted → overflow. The prompt box (`PromptBoxV2.vue`) also carries desktop padding `p-4 pb-8` (32px bottom) straight onto mobile. |
| 3 | Tab shows UUID | `r/[id]/index.vue` set **no** page title (no `useHead`), and there is no global `titleTemplate`, so the browser falls back to the URL's last path segment — the report UUID. |
| 4 | Input zoom | iOS/WebKit auto‑zooms (then scroll‑jumps) when focusing an `<input>`/`<select>`/`<textarea>` whose `font-size` is `< 16px`. Most inputs use `text-sm` (14px). |
| 5 | No mobile nav | `layouts/default.vue` sidebar is `-translate-x-full … sm:translate-x-0` with no mobile toggle; content is `sm:ms-60` (full‑width on mobile) but nothing exposes the nav. |

---

## The fix

Source changes (all in `frontend/`):

- **`assets/css/mobile.css`** (new, registered in `nuxt.config.ts`) — global,
  mobile‑only rules: force `font-size: 16px` on inputs/selects/textareas at
  `≤640px` (kills the iOS focus‑zoom app‑wide without changing desktop), and
  clamp `html,body` to `overflow-x:hidden; max-width:100vw`. **(#4)**
- **`pages/r/[id]/index.vue`** — responsive top bar: `px-[200px] → px-11 sm:px-[200px]`,
  "Back to app" / "Fork" / "Edit Report" / "Refreshed" labels become
  `hidden sm:inline` (icon‑only on mobile), tighter `end-2/start-2` gutters;
  `h-screen → h-dvh` + `overflow-hidden`; and a `useHead` that titles the tab
  with the artifact title (then report title, then `Report`). **(#1, #2, #3)**
- **`components/report/SplitScreenLayout.vue`** and **`pages/reports/[id]/index.vue`** —
  `h-screen → h-dvh`; report page also gets a `useHead` title. **(#2, #3)**
- **`components/prompt/PromptBoxV2.vue`** — `p-4 pb-8 → p-3 pb-3 sm:p-4 sm:pb-8`
  (tight on mobile, unchanged on desktop). **(#2)**
- **`composables/useSidebar.ts`** + **`layouts/default.vue`** — a mobile drawer:
  new `mobileOpen`/`openMobile`/`closeMobile` state; a `sm:hidden` mobile top
  bar (hamburger · logo · new‑report), a backdrop, the sidebar slides in on
  `mobileOpen`, and content top‑padding clears the bar. The bar is suppressed
  on the immersive `/reports/{id}` page (it has its own header and is `h-dvh`).
  Desktop is untouched. **(#5)**

`h-dvh` (dynamic viewport height) is the correct fix for #2 on real devices;
in a headless browser `100vh == 100dvh` so the overflow only reproduces with
real browser chrome — the change is still applied because it is the right one.

---

## Environment setup (fresh sandbox)

Backend (Python 3.12) + frontend (Node 22). Artifacts render inside an iframe
that loads vendored JS libs which are **git‑ignored** and downloaded at build
time — you must fetch them or the artifact iframe renders blank.

```bash
# Backend
cd backend
uv sync --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_ENCRYPTION_KEY="dev-encryption-key-32-bytes-long!"
mkdir -p db && uv run alembic upgrade head
uv run python main.py            # :8000

# Vendored artifact libs (React/Babel/echarts/tailwind) — REQUIRED for the
# artifact iframe to render:
bash scripts/download-vendor-libs.sh frontend/public/libs

# Frontend
cd frontend
yarn install
yarn dev                         # :3000
```

Headless Chromium is pre‑installed at
`/opt/pw-browsers/chromium-1194/chrome-linux/chrome`; the harness scripts pass
it via `executablePath` (the pinned `@playwright/test` wants a different build).

---

## Loop — reproduce + screenshot (before/after)

Runnable harness in `frontend/tests/mobile-ui/`:

```bash
cd frontend

# 1. Create an admin user + org via the real signup flow (saves storage state)
node tests/mobile-ui/signup.mjs

# 2. Seed a PUBLIC report + page artifact (Hebrew title, bar‑chart dashboard)
#    and dismiss onboarding so the authenticated screens render.
cd ../backend && python3 seed_mobile_ui.py     # prints REPORT_ID / PUBLIC_URL
#    (onboarding is dismissed by flipping organization_settings.config.onboarding)

# 3. Sweep the main screens at an iPhone viewport (390×844). Writes PNGs +
#    metrics.json (per‑screen vertical‑scroll / horizontal‑overflow / title).
cd ../frontend
REPORT_ID=<id> PHASE=before node tests/mobile-ui/shots.mjs   # baseline
#    …apply fixes / restart dev server…
REPORT_ID=<id> PHASE=after  node tests/mobile-ui/shots.mjs   # verify

# 4. Optional: drawer‑open shot, desktop no‑regression shots, and labeled
#    before/after comparison composites.
node tests/mobile-ui/drawer.mjs
node tests/mobile-ui/desktop.mjs
node tests/mobile-ui/compose.mjs
```

### Observed

- **#1 top bar** — before: `Back to app · Report · Data(0) · Refreshed 8m ago · ✕`
  overlap into an unreadable pile. after: `← · [Report] Data(0) · ✕`, no overlap.
- **#3 title** — `metrics.json` `title` for `03-public-artifact` goes from
  `""` (→ browser shows the UUID) to `מכירות לפי קטגוריה (ז'אנר)`.
- **#4 zoom** — computed `font-size` of `#email` on the login screen: `14px → 16px`.
- **#2 prompt box** — mobile bottom padding drops from 32px to 12px; the box is
  no longer clipped.
- **#5 nav** — `button[aria-label="Open menu"]` now present on standard pages;
  the drawer slides in with the full nav + backdrop. Dashboards/Monitoring/etc.
  are reachable on mobile.
- **No desktop regression** — at 1280px the sidebar is fully visible and the
  mobile bar is `sm:hidden` (verified via `tests/mobile-ui/desktop.mjs`).

### Known / out of scope

- The `/reports/{id}` chat view can still show a ~40px overflow **when the
  "Configure your LLM" onboarding banner is visible** (banner 40px atop an
  `h-dvh` page). This is the pre‑existing banner‑vs‑full‑height tension
  (`useTopBanner` exists precisely so full‑height views can subtract it) and is
  independent of these mobile changes; it disappears once an LLM is configured.
- Home (`/`) and the Reports list render an empty state in this seeded env
  (no data source / agents) — unchanged before vs after.
