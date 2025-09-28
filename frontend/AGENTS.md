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