---
name: localization
description: The locale/i18n architecture of bagofwords — catalogs, resolution order, RTL, backend contracts — and the procedures for adding strings, adding a locale, or translating UI. Use when adding/changing user-facing strings, working on RTL, emails, localized errors, or anything locale-related.
---

# Localization — architecture & procedures

Authoritative deep-dive: `docs/design/i18n.md`. This skill is the working map.

## Locales

Catalogs live at the repo root: `locales/{en,es,he,fr,sv,ar,ru,de,pt,it}.json`
(10 locales). `en` is the **authoritative** catalog — its key set and ordering
define the shape; the target is **identical key structure** in every catalog.
`es` and `he` are the actively maintained translations; the other seven are
known to lag behind `en` (run the sync check below to see current drift).
`he` is the RTL reference — anything RTL must work for Hebrew; `ar` is also
RTL (plugin whitelists `he/ar/fa/ur`).

Hebrew vocabulary conventions: "הנחיות" (never "הוראות"), "גלריה" (never
"לוח מחוונים"), "בדיקות איכות" (never "הערכות").

## Resolution order (highest wins)

1. `X-Locale` request header — test-only override; must be in `enabled_locales`.
2. Per-user choice — `localStorage["bow.locale"]` on the client.
3. Organization locale — `OrganizationSettings.config["locale"]`, set via
   `/settings/general`, served by `GET/PUT /api/organization/locale`
   (PUT gated by `manage_settings`).
4. System default — `bow_config.i18n.default_locale` (`en`).
   Enabled set: `settings.bow_config.i18n.enabled_locales`
   (`backend/app/settings/bow_config.py`).

## Architecture map

**Frontend** (`vue-i18n@9`, composition mode):
- `frontend/plugins/i18n.ts` — creates the global instance, imports catalogs,
  applies persisted `bow.locale`, exposes `$setLocale`, sets `<html lang>` and
  `<html dir>`.
- `frontend/layouts/default.vue` — after session ready, fetches
  `GET /api/organization/locale` and applies `effective_locale` only when no
  personal override exists.
- `frontend/composables/useErrorMessage.ts` — maps backend `error_code` +
  `params` to `t('errors.<code>', params)`, falling back to server `detail`.
- RTL: Tailwind **logical properties only** (`ms-*`, `me-*`, `ps-*`, `pe-*`,
  `start-*`, `end-*` — never `ml-*`/`pl-*`/`left-*`). Global icon flips and
  third-party overrides in `frontend/assets/css/rtl.css`; opt out per element
  with `rtl-no-flip`. Never `dir="auto"` on empty contenteditable — bind
  `:dir` to the active locale.

**Backend**:
- Dependencies: `get_current_locale(request)` (unauthed-safe, header→default),
  `get_org_locale(request, organization)` (header→org→default),
  `_locale_from_org(organization)` for services holding an org object.
- Typed errors (`backend/app/errors/`): raise `AppError.*(ErrorCode.X, ...)`,
  never bare `HTTPException`. New code = enum entry in `app/errors/codes.py`
  **+** a matching `errors.<code>` key in every locale catalog.
- Emails: `app/services/email_renderer.py` + `email_strings.py`, shared Jinja
  templates in `app/templates/emails/*.jinja2` honoring `lang`/`dir`. Keep
  substitutions HTML-escaped — `description` in `share.html.jinja2` is `| safe`.
- LLM prompts: `app/ai/prompt_language.py` injects a "respond in {language}"
  directive for **conversational** agents only (planner/answer/judge/reporter/
  suggest_instructions). Code/artifact agents stay English so SQL, identifiers,
  and JSON fields never get translated. Returns `""` for `en`.
- Public boot config: `GET /api/config/i18n`.

## Procedures

**Adding a string**: add the key to `locales/en.json` under the right
namespace, then add the same key path to **all** other catalogs in one pass.
Use named interpolation (`t('key', { name })`) — never concatenation; word
order varies by language. Locale-reactive label/option arrays in
`<script setup>` must be wrapped in `computed(() => [...])`.

Run the sync check before and after your change — the catalogs have known
pre-existing drift, so the rule is: **your diff must not increase any
`missing`/`extra` count** (and should reduce them where cheap):

```bash
python3 - <<'EOF'
import json, pathlib
def flatten(d, p=''):
    out = set()
    for k, v in d.items():
        f = f'{p}.{k}' if p else k
        out |= flatten(v, f) if isinstance(v, dict) else {f}
    return out
cats = {f.stem: flatten(json.loads(f.read_text())) for f in pathlib.Path('locales').glob('*.json')}
en = cats.pop('en')
for loc, keys in sorted(cats.items()):
    missing, extra = en - keys, keys - en
    print(f'{loc}: missing={len(missing)} extra={len(extra)}')
    for k in sorted(missing)[:10]: print(f'  - {k}')
print(f'en: {len(en)} keys')
EOF
```

**Adding a locale**: create `locales/<code>.json` mirroring `en.json`'s full
shape; add the code to `enabled_locales` in `backend/app/settings/bow_config.py`
and the import in `frontend/plugins/i18n.ts`; if RTL, add it to the plugin's
RTL whitelist; extend `frontend/tests/i18n/locale-sweep.spec.ts`.

**Testing**: `cd frontend && npx playwright test --config=playwright.i18n.config.ts`
runs the locale sweep (asserts `html[lang]`/`html[dir]` flip, strings render,
no `{{…}}`/unresolved key paths leak, no `[intlify]` console warnings). Keep it
green for any catalog or locale change. For RTL-visible changes, capture a
Hebrew screenshot per the **ui-evidence** skill.

## Pitfalls that recur

- Adding a key to `en.json` only → sync check fails for the other 9 catalogs.
- Hard-coded literals in toasts/`confirm()`/script-side strings — every
  user-facing string comes from the catalog.
- Keying CSS-class maps on localized labels — key on canonical identifiers.
- Hand-formatted dates/numbers — always `Intl.*` with `locale.value`.
- Physical CSS properties sneaking in via copy-paste — logical only.
