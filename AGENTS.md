# AGENTS Guidelines (repo root)

See `frontend/AGENTS.md` and `backend/AGENTS.md` for the per-side guides, and
`backend/tests/AGENTS.md` before writing or changing any backend test.

### Issues & PRs — read the authoring standard first

Before you **open a GitHub issue** or **write a PR description**, read the
matching standard **in full** and follow it. These are dual-audience artifacts
(a maintainer must be able to *decide* from the top; a future fixer/agent must
be able to *navigate and reproduce*), distilled from this repo's own issues/PRs:

- **`.claude/templates/issue_schema.md`** — how to open an issue: title
  `[Severity] area: precise outcome`, **user-impact first** (a named persona,
  what they see, why it hurts), a single **root cause with `file:line`**,
  **quoted evidence** (never "I think"), the **"tell"** that proves it's a real
  bug vs intended, honestly-**bounded scope** (what's NOT affected), an in-repo
  **correct-reference pattern**, a **minimal→fuller proposed fix**, and a
  code-derived **wireframe only for visual bugs**. Severity is a worded judgment,
  not a score.
- **`.claude/templates/PR_DESCRIPTION_STANDARD.md`** — the PR body shape:
  reviewer **decision box**, a product/customer **executive summary**, both a
  **product-impact and a technical visual**, an **evidence KPI**, and a
  **context map** — ordered so a reviewer can decide from the first screen and a
  future agent can navigate the change from the bottom.

### Agent skills

Reusable agent procedures live in `.agents/skills/<name>/SKILL.md`
(tool-agnostic location). Read and follow the matching skill before starting
one of these tasks:

- **sandbox-feedback-loop** — build a runnable reproduce→fix→verify loop for
  any bug/feature; reports land in `docs/feedback-loops/`.
- **qa** — map all user-facing functionality (`docs/qa/functionality-map.md`),
  then live-test flows against a running stack.
- **ui-evidence** — capture the **mandatory** before/after screenshots (and
  GIFs/videos for flows) for any change touching
  `frontend/{pages,components,layouts,assets}/**`.
- **docs-update** — refresh docs.bagofwords.com (Mintlify MCP) with text +
  screenshots after user-facing changes ship.
- **localization** — the locale/i18n architecture end to end; follow it when
  adding strings, locales, or touching anything RTL.
- **add-connection-type** — add a data source/connector (client, config +
  credentials schemas, registry entry, tests) with verification steps.
- **add-llm-provider-or-model** — extend the LLM catalog; model id, pricing,
  and context window must be verified against official docs first.
- **release-notes** — bump `VERSION` and write a minimal, user-facing
  `CHANGELOG.md` entry after a user-facing change ships.

Shared setup scripts (usable by humans too): `tools/agent/boot_stack.sh`
(run the full stack like CI), `tools/agent/seed_org.py` (seed org/users/demo
data through the real API), `tools/agent/capture.mjs` (screenshots/videos).

### Locale / i18n (system overview)

Three locales ship today: `en` (default), `es`, `he`. Hebrew is the RTL
reference — anything RTL must work for `he`.

**Resolution order** (highest priority wins):

1. `X-Locale` request header — test-only override; must be in `enabled_locales`.
2. Per-user personal choice — lives in `localStorage.bow.locale` on the
   client. (A server-backed `users.locale` column is on the profile-page
   design roadmap but not yet built.)
3. Organization locale — stored in `OrganizationSettings.config["locale"]`,
   set via the picker in `/settings/general`.
4. System default — `bow_config.i18n.default_locale` (`en`).

**Catalogs** live at the repo root: `locales/en.json`, `locales/es.json`,
`locales/he.json`. All three must have identical shape — key drift fails
the sync check in `docs/design/i18n.md`. Hebrew vocabulary convention:
"הנחיות" (never "הוראות"), "גלריה" (never "לוח מחוונים"),
"בדיקות איכות" (never "הערכות").

**End-to-end flow on login:**

```
frontend/plugins/i18n.ts        registers createI18n, applies personal
                                 bow.locale if present, exposes $setLocale
frontend/layouts/default.vue    after session ready, fetches
                                 GET /api/organization/locale and applies
                                 effective_locale when no personal override
GET/PUT /api/organization/locale FastAPI routes; PUT requires manage_settings
backend/app/services/...         emails, LLM prompts, and typed errors each
                                 pull their own locale (see backend/AGENTS.md)
```

See `docs/design/i18n.md` for authoring rules, ESLint-plugin-vue-i18n
config, the catalog sync check, and RTL conventions.
