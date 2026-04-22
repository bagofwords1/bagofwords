# AGENTS Guidelines (repo root)

See `frontend/AGENTS.md` and `backend/AGENTS.md` for the per-side guides.

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
