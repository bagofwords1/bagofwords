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

### Design tokens (read before writing any markup)

Visual decisions live in `tailwind.config.ts` and `assets/css/tokens.css`,
not in the component you are editing. Reach for a token before a raw shade.

**Text color.** Use the semantic classes, never a grey shade:

| Use | Class | Light | Dark |
|---|---|---|---|
| Headings, primary text | `text-ink` | 17.74:1 | 16.98:1 |
| Body | `text-ink-2` | 10.31:1 | 14.33:1 |
| Labels, timestamps, helper text | `text-ink-muted` | 4.97:1 | 6.99:1 |
| Placeholders, disabled only | `text-ink-subtle` | 3.10:1 | 3.67:1 |

These flip with the theme, so **a semantic text class takes no `dark:`
variant** — adding one re-introduces the bug they exist to fix. Surfaces
(`bg-surface`, `bg-surface-subtle`, `bg-surface-muted`, `bg-surface-elevated`)
and borders (`border-line`, `border-line-strong`) work the same way.

Never use `text-gray-400` for light-mode text: it is 2.54:1 on white, below
WCAG AA and below the 3:1 large-text floor. 1,938 call sites were migrated off
it; `dark:text-gray-400` is fine and stays.

**Type scale.** Nine steps, no arbitrary values — `text-[11px]` and friends
are gone and must not come back. `text-2xs` (11px) is the floor and is for
dense metadata only (badges, table chrome, the sidebar ticker); `text-xs`
(12px) and `text-sm` (14px) carry ordinary UI text.

**Radius.** Two, not three: `rounded`/`rounded-md` are both 6px (controls),
`rounded-lg` is 8px (cards, panels), `rounded-xl` 12px (modals). `rounded-sm`
(4px) is only for affordances 16px or smaller. `rounded-composer` (20px) is
the composer surface and nothing else — it is named rather than added to the
scale so that Tailwind's stock `rounded-2xl` keeps its 16px on the 21 call
sites that already use it.

**The conversation surface.** The transcript, the composer, the landing
composer, the shared-link view and the tool cards inside a transcript all
take `max-w-chat` (`--bow-measure-chat`). One token, because the box you
type into must be the same width as the text you read back — they had
drifted to 672px and ~53vw, so the composer resized the moment you pressed
enter. The measure is `48 x` the chat font rather than a fixed width, so
line length stays ~96 characters at every reader step; at the default it
computes to 672px.

Chat copy uses a three-step ramp, all derived from one variable so that a
single message stops mixing unrelated systems: `text-chat` for body
(the `sm` step, 14px, times the reader's preference), `text-chat-2` for
code, collapsed reasoning and tool-card labels (13px), `text-chat-3` for
badges, ids and durations (11px). 14px is measured against comparable
agent interfaces, which sit at 13-14px — general chat products run higher
but are the wrong comparison for a transcript carrying tables and query
results. Never pin a px font size inside a conversation: a pinned 13px
stays put while body copy scales, which is the defect the ramp exists to
prevent. Outside a conversation the fixed steps still apply.

Those two are *reader* preferences, stored per browser
(`localStorage['bow.chat.width' | 'bow.chat.textScale']`, see
`composables/useChatDisplay.ts`), applied to `<html>` by
`plugins/chat-display.client.ts`, and set from `ChatDisplayMenu` in the
report header. Reading size is not an organization setting: nobody should
need `manage_settings` to make text bigger. If you add a surface that is part
of a conversation, use the tokens — do not reintroduce a literal width or a
fixed chat font size.

**Motion.** A bare `transition` already inherits 180ms and the standard
curve — do not add `duration-*`/`ease-*` unless you mean something other than
the default. Use `duration-fast` (120ms) / `duration-slow` (280ms) when you
do. Avoid `transition-all`; name the property. Loading shimmer is the single
`.bow-shimmer` class, not a per-component `@keyframes`.

There are **no route transitions**, and adding them is not a one-line change.
Vue's `<Transition>` only animates a component with a single *element* root,
and a leading HTML comment already makes a template a Fragment. 15 of the 68
pages are Fragments today — `index.vue`, `reports/[id]/index.vue`,
`r/[id]/index.vue` and all four `users/*` pages among them — and enabling
`app.pageTransition` blanks them. See the note in `nuxt.config.ts`.

**Layering.** Use the named scale (`z-dropdown`, `z-sticky`, `z-overlay`,
`z-modal`, `z-popover`, `z-tooltip`, `z-toast`). Never invent a `z-[1050]`.

**Press feedback and focus** are global (`assets/css/tokens.css`): every
`button` and `[role="button"]` gets an active-state nudge. Opt an element out
with `.no-press` where the nudge is wrong (drag handles, gridstack items,
resizers).

The focus ring is a **zero-specificity fallback**, written with `:where()` so
it sits below every class in the cascade. Any `outline-*`, `ring-*` or
`focus-visible:*` utility you write beats it automatically — you never need to
fight it, and `outline-none` means what it says. It paints only on links,
buttons and `[tabindex]` elements that style focus nowhere else. Form controls
are excluded on purpose: Nuxt UI rings them, and the app's own inputs are
often deliberately `outline-none` inside a styled wrapper. If you need a focus
style, write it on the element — do not widen this rule.

**Numbers.** Add `.nums` to table cells and KPIs so digits are tabular and
columns stop shifting between rows.

If you change a token's value, run `node scripts/check-contrast.mjs` — it
recomputes every pairing the stylesheet asserts and fails on a regression.

**Prefer `UButton`/`UInput`/`USelectMenu` over hand-rolled `<button>` and
`<input>`.** Around 75% of the app's buttons and 89% of its inputs still
bypass Nuxt UI, which is why control heights and focus states disagree
between screens; defaults for the component library live in `app.config.ts`.
Do not add to that pile.

### Adding a feature (quick checklist)
1. Create/extend a page under `pages/` and supporting UI in `components/`.
2. Add data access via `composables` (prefer `useMyFetch` to inherit proxy/baseURL).
3. Apply route guards using `middleware` if the feature requires auth/permissions/onboarding.
4. Wire WebSocket features to `runtimeConfig.public.wsURL` when needed.
5. Add/adjust e2e tests under `frontend/tests` and Playwright config as needed.
6. Capture before/after UI evidence for the PR — see below.

### UI evidence (mandatory)

Any change touching `pages/`, `components/`, `layouts/`, or `assets/` must
include before/after **screenshots** in the PR description — and a short
**GIF/video** when the change is a flow, animation, or streaming UI. Skip only
for refactors with zero visual effect, and say so in the PR description. Full
capture procedure: `.agents/skills/ui-evidence/SKILL.md`. For RTL-affecting
work, include a Hebrew (`he`) capture as well.

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