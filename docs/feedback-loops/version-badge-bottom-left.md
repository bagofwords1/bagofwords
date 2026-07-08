# Feedback Loop — "put the version back at the bottom left, and on click show the changelog"

The app version used to render at the bottom-left of the sidebar in the default
layout. Commit `181ebb2` ("UI") removed that `<li>` and instead appended a
disabled `v{version}` row to the user dropdown. The request: move the version
back to the bottom-left of `layouts/default.vue` (also visible + centered when
the sidebar is collapsed — the old element was hidden when collapsed), make
clicking it open the changelog modal, and change the changelog modal so only the
latest release is expanded by default (older ones collapsible per-version).

## Root cause (validated)

Not a bug — a regretted UX change. The removal is visible in
`git show 181ebb2 -- frontend/layouts/default.vue`:

- Removed: `<li v-if="version && !isCollapsed">` bottom-of-sidebar version
  (previously after the user-dropdown `<li>` in the bottom `<ul>`,
  `frontend/layouts/default.vue`).
- Added: `groups.push([{ label: 'v${version}', isVersion: true, disabled: true }])`
  in `userDropdownItems` plus an `item.isVersion` branch in the dropdown slot.

The version value comes from `useRuntimeConfig().public.version`, hydrated from
`GET /api/settings` by `frontend/plugins/settings.ts:12`. The changelog modal
(`frontend/components/ChangelogModal.vue`) rendered **every** version expanded —
232 releases of entries in one scroll.

## Loop A — deterministic reproduction (Playwright, no external services)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py     # admin@example.com / Password123!
# dismiss onboarding for the seeded org (PUT /api/organization/onboarding {"dismissed": true})
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
cd frontend
# record an admin storageState once (sign-in via #email/#password), then:
npx playwright test tests/home/version-badge.spec.ts   # features project in CI
```

The spec (`frontend/tests/home/version-badge.spec.ts`) asserts:

1. `button[name="app-version"]` is visible in the sidebar and matches `/^v\d+\.\d+/`;
2. clicking it opens the changelog modal;
3. only the first (latest) version row has `aria-expanded="true"` and visible
   entries — the second row is collapsed with no entries in the DOM;
4. an older version can be toggled open and closed;
5. after collapsing the sidebar the badge is still visible and still opens the
   modal.

Observed on base (`26d0eac`, change stashed):

```
2 failed
  tests/home/version-badge.spec.ts:18:1 › version badge shows at the sidebar bottom-left and opens the changelog
  tests/home/version-badge.spec.ts:42:1 › version badge stays visible and clickable when the sidebar is collapsed
  (waiting for locator('button[name="app-version"]') — element not found)
```

## The fix

- `frontend/layouts/default.vue` — new `<li>` after the user dropdown in the
  bottom `<ul>`: a `button[name="app-version"]` showing `v{{ version }}`,
  `justify-center` when `isCollapsed`, `@click="showChangelogModal = true"`.
  The `isVersion` dropdown row and its slot branch are removed (the version
  moved out of the dropdown, back to the sidebar).
- `frontend/components/ChangelogModal.vue` — version headers became toggle
  buttons (`aria-expanded`, rotating chevron, entry count when collapsed);
  `expandedVersions` starts as `{0}` (latest only) after each fetch.
- `locales/{en,es,he}.json` — new `changelog.changeCount` plural key.

Re-run of the same spec with the change applied:

```
2 passed (11.9s)
  ✓ version badge shows at the sidebar bottom-left and opens the changelog
  ✓ version badge stays visible and clickable when the sidebar is collapsed
```

UI evidence (before/after, expanded/collapsed/RTL):
`media/pr/claude-version-display-changelog-wz5o2c/`.

## What this proves / regression notes

- The badge renders in both sidebar states from live `/api/settings` data and
  opens the modal; the modal defaults to latest-only-expanded against the real
  232-version `/api/changelog` payload. The spec survives as a regression test
  in the `features` Playwright project (`**/home/**`).
- Pre-existing, unrelated: `locales/es.json` / `locales/he.json` are missing
  234 keys vs `en.json` (verified identical drift on base with the change
  stashed; `changelog.changeCount` itself is present in all three).
