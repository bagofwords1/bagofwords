### AGENTS Guidelines for frontend

### Purpose
Concise overview of `@frontend/` (Nuxt 3 SPA) with structure, key building blocks, and how it talks to the FastAPI backend via the built-in proxy.

### Structure
- **root**
  - `nuxt.config.ts`: App configuration. Disables SSR, registers modules (UI, auth, editor, charts, proxy, Intercom, Monaco), sets `runtimeConfig.public.baseURL` to `/api` and `wsURL` to `/ws/api`, and configures an HTTP/WS proxy to the FastAPI backend.
  - `package.json`, `tsconfig.json`, `playwright.config.ts`: Tooling and tests.
  - `public/`: Static assets served as-is.

- **pages/**
  - File-based routing. Top-level app screens (e.g., `dashboard.vue`, `index.vue`, `reports/[id]/index.vue`, `users/*`, `settings/*`, `integrations/*`, `onboarding/*`, etc.).

- **components/**
  - Reusable UI and feature widgets.
  - Notable areas:
    - `dashboard/`: Widgets, themes, registry, table and text views.
    - `console/`: Observability dashboards and trace UIs.
    - `datasources/`, `excel/`, `prompt/`, `report/`, and various modals (e.g., `ShareModal.vue`, `GitRepoModalComponent.vue`).

- **layouts/**
  - App scaffolding (navigation, headers, footers, shells) shared across pages.

- **middleware/**
  - `auth.global.ts`: Enforces authentication and routes to sign-in/sign-up when needed.
  - `permissions.global.ts`: Guards routes based on role/permission checks.
  - `onboarding.global.ts`: Redirects users through required onboarding steps.

- **composables/**
  - `useMyFetch.ts`: Centralized fetch wrapper; uses `runtimeConfig.public.baseURL` (`/api`) so calls automatically go through the proxy to FastAPI.
  - `useOrganization.ts`, `usePermissions.ts`, `useOnboarding.ts`: Session/org context and gatekeeping helpers.
  - `useExcel.ts`: Helpers for Excel workflows.
  - `useDebouncedRef.ts`: Utility for debounced local state.

- **plugins/**
  - Client-only integrations (e.g., `vue-draggable-resizable.client.js`, `vue-flow.client.js`).

- **modules/**
  - Nuxt modules (if any) to extend build/runtime behavior.

### Data flow (typical request)
`components/*` or `pages/*` → `composables` (e.g., `useMyFetch`) → HTTP calls to `/api/*` (proxied to FastAPI) and/or WebSocket to `/ws/api` → render results in components.

### Auth & session
- Uses `@sidebase/nuxt-auth` with local provider; endpoints are proxied (e.g., `/auth/jwt/login`, `/users/whoami`).
- Token persisted via cookie; session auto-refreshed on window focus per config.

### Proxy behavior (`nuxt.config.ts`)
- HTTP: Requests to `/api/*` are proxied to `http://127.0.0.1:8000` (FastAPI).
- WebSocket: Requests to `/ws/api` are proxied to `ws://127.0.0.1:8000` with correct upgrade headers.
- Public runtime config provides `baseURL: '/api'` and `wsURL: '/ws/api'` so app code doesn’t hardcode backend origins.

### Conventions
- Prefer composables for server I/O and shared state; keep components presentational where possible.
- Use file-based routing under `pages/` and global route guards in `middleware/` for auth/permissions/onboarding.
- Keep chart/editor-heavy features lazy where possible to optimize payload.

### Adding a feature (quick checklist)
1. Create/extend a page under `pages/` and supporting UI in `components/`.
2. Add data access via `composables` (prefer `useMyFetch` to inherit proxy/baseURL).
3. Apply route guards using `middleware` if the feature requires auth/permissions/onboarding.
4. Wire WebSocket features to `runtimeConfig.public.wsURL` when needed.
5. Add/adjust e2e tests under `frontend/tests` and Playwright config as needed.

### Locale / i18n

- **Library**: `vue-i18n@9` in composition mode (`legacy: false`). Plugin at
  `plugins/i18n.ts` imports the three catalogs at `/locales/{en,es,he}.json`,
  creates the global instance, and exposes `$setLocale(code)` via
  `nuxtApp.provide`. It also sets `<html lang>` and `<html dir>` (RTL for
  `he`/`ar`/`fa`/`ur`).
- **Catalog authoring**: always add the same key path to en/es/he in one
  pass. A catalog sync check (`docs/design/i18n.md`) enforces identical
  shape across locales. Use named interpolation (`t('key', { name })`) —
  never concatenation.
- **In templates**: `$t('ns.key')` or `:placeholder="$t(...)"`. For inline
  markup, use `<i18n-t keypath="..." tag="span">` with named slots.
- **In `<script setup>`**: `const { t, locale } = useI18n()`. Wrap
  locale-reactive label/option arrays in `computed(() => [...])` so
  `USelectMenu` etc. relocalize on language switch.
- **Hydration flow**: `layouts/default.vue` calls
  `GET /api/organization/locale` via `useMyFetch` after mount and applies
  `effective_locale` only when `localStorage.bow.locale` is unset. The
  picker in `pages/settings/general.vue` PUTs to the same endpoint and
  calls `$setLocale` so the admin sees the flip immediately.
- **Error messages**: `composables/useErrorMessage.ts` reads
  `error_code` / `params` from an `AppError` response and resolves to
  `t('errors.<error_code>', params)`, falling back to the server-provided
  `detail` if no catalog entry matches.
- **RTL**: prefer Tailwind logical properties (`ms-*`, `me-*`, `ps-*`,
  `pe-*`, `start-*`, `end-*`) over physical (`ml-*`, `pl-*`, `left-*`).
  Global directional-icon flip and third-party overrides (notably
  `markstream-vue`'s hard-coded `padding-left` on list markers) live in
  `assets/css/rtl.css`. Opt out on a specific element with `rtl-no-flip`.
  Never use `dir="auto"` on empty contenteditable — it resolves to LTR
  per spec; bind `:dir` to the active locale instead.
- **Tests**: `tests/i18n/locale-sweep.spec.ts` (run with
  `playwright.i18n.config.ts`) covers en/es/he on unauthenticated pages
  (`/i18n-smoke`, `/users/sign-in`) — asserts `html[lang]`/`html[dir]` flip,
  expected strings render, no `{{…}}` or unresolved key paths leak, no
  `[intlify]` console warnings. Keep this green when adding locales or
  catalog entries.