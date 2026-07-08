# Feedback Loop — "when a new instruction is added then on the right it is empty" / `RangeError: Adding different instances of a keyed plugin (plugin$1)`

Opening an instruction detail (Knowledge Explorer `/agents/instructions/<id>`,
or the Report agent panel) rendered the title and metadata sidebar but a
completely blank body, and the console showed
`Uncaught (in promise) RangeError: Adding different instances of a keyed plugin (plugin$1)`
from a Vite dep chunk. Both symptoms are one bug: the tiptap editor crashes
while mounting, so `<EditorContent>` renders an empty div. Dev-mode only
(`yarn dev` / `boot_stack.sh --dev`); production builds bundle once and are
unaffected.

## Root cause (validated)

Two copies of `prosemirror-state` were loaded at runtime in dev:

- `nuxt-tiptap-editor` puts every tiptap package it registers
  (`@tiptap/core`, `@tiptap/vue-3`, `@tiptap/starter-kit`, …) into
  `build.transpile`, which Nuxt translates to Vite `optimizeDeps.exclude` —
  those are served as raw ESM, resolving `prosemirror-state` to
  `/node_modules/prosemirror-state/dist/index.js`.
- The app's own direct deps `@tiptap/extension-mention` / `@tiptap/suggestion`
  (`frontend/package.json`) were NOT excluded, so Vite pre-bundled them with a
  second, inlined copy of `prosemirror-state` (`…/.cache/vite/client/deps/chunk-*.js`).

`prosemirror-state` auto-generates keys for unkeyed plugins from module-level
state (`plugin$`, `plugin$1`, …). Two module instances run two independent
counters, so plugins created by copy A collide with plugins created by copy B
when the counters align — `EditorState.reconfigure` throws the RangeError
inside `Editor.createView`, `useEditor()` leaves the editor ref undefined, and
`InstructionEditor.vue` rendered nothing (the placeholder is edit-mode-only).

Repro diagnostics (pre-fix), from the loop below:

```
prosemirror-state copies loaded by the page: 2
  http://localhost:3000/_nuxt/node_modules/.cache/vite/client/deps/chunk-5YCZVOBW.js?v=31a300d4
  http://localhost:3000/_nuxt/node_modules/prosemirror-state/dist/index.js?v=31a300d4
keyed-plugin RangeErrors seen: 1
  Adding different instances of a keyed plugin (plugin$1)
instruction body visible: false
.tiptap-prose innerText: "(no .tiptap-prose element)"
```

(`chunk-5YCZVOBW.js` is byte-for-byte the chunk name from the original report.)

## Loop A — deterministic reproduction (no external services)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py     # admin@example.com / Password123!
cd ../frontend
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# One-time: record the admin storage state (UI login → tests/config/admin.json)
node - <<'EOF'
import('@playwright/test').then(async ({ chromium }) => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
  const page = await (await browser.newContext()).newPage();
  await page.goto('http://localhost:3000/users/sign-in', { waitUntil: 'commit', timeout: 120000 });
  await page.waitForSelector('input[type="email"], #email', { timeout: 60000 });
  await page.fill('input[type="email"], #email', 'admin@example.com');
  await page.fill('input[type="password"], #password', 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForURL(u => !u.pathname.includes('/users/sign-in'), { timeout: 60000, waitUntil: 'commit' });
  await page.waitForTimeout(2000);
  await page.context().storageState({ path: 'tests/config/admin.json' });
  await browser.close();
});
EOF

# Minimal config so the run skips global setup (which signs up a fresh user)
cat > /tmp/pw-single.config.ts <<'EOF'
import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  timeout: 120 * 1000,
  use: {
    headless: true,
    baseURL: 'http://localhost:3000',
    storageState: 'tests/config/admin.json',
    launchOptions: { executablePath: '/opt/pw-browsers/chromium' },
  },
});
EOF

npx playwright test tests/instructions/instruction-editor-renders.spec.ts -c /tmp/pw-single.config.ts --reporter=line
```

Observed on pre-fix code (instruction title visible, body blank):

```
Error: expect(locator).toContainText(expected) failed
Locator: locator('.tiptap-prose').first()
Expected substring: "editor-render-check-…"
Received: <element(s) not found>
…
keyed-plugin errors:
RangeError: Adding different instances of a keyed plugin (plugin$1)
  1 failed
```

The spec survives as the regression test:
`frontend/tests/instructions/instruction-editor-renders.spec.ts` — it seeds an
instruction through the API, deep-links to `/agents/instructions/<id>`, and
asserts (a) the tiptap editor actually mounts with the text visible and (b) no
keyed-plugin error fires. In CI it runs against the production build, where it
guards the general "instruction body renders" invariant.

## The fix

1. `frontend/nuxt.config.ts` — `vite.optimizeDeps.exclude` for the whole
   tiptap/prosemirror family (`@tiptap/extension-mention`, `@tiptap/suggestion`,
   `@tiptap/pm`, all `prosemirror-*` packages). Dev now serves exactly one copy
   of each module — the same strategy nuxt-tiptap-editor already applies to the
   packages it registers. No effect on production builds.
2. `frontend/package.json` — removed the unused legacy `tiptap@^1.32.2` (v1)
   dependency, which dragged stale prosemirror ranges into the lockfile.
3. `frontend/components/instructions/InstructionEditor.vue` — defense in depth:
   the AutoDir ProseMirror plugin now has an explicit `PluginKey`
   (auto-generated keys are what collide across duplicated modules), and the
   editor is constructed in a guarded `onMounted` (replacing `useEditor`): if
   construction ever throws again, the component falls back to the raw-markdown
   textarea instead of rendering a blank pane.

Re-run of the same loop after the fix:

```
prosemirror-state copies loaded by the page: 1
  http://localhost:3000/_nuxt/node_modules/prosemirror-state/dist/index.js?v=99f1d071
keyed-plugin RangeErrors seen: 0
instruction body visible: true
.tiptap-prose innerText: "When a user requests music data by artist name, …"
  1 passed
```

Also verified post-fix: edit mode still types into the tiptap doc (Edit →
keyboard input reaches `.tiptap-prose`, `update:modelValue` fires), SPA
navigation (`/agents` → instruction) behaves the same as a direct deep link,
and `yarn build` still succeeds.

## What this proves / regression notes

- The blank instruction body and the keyed-plugin RangeError were one dev-only
  bug: duplicated `prosemirror-state` module instances from mixed Vite
  pre-bundling, not a data problem (the instruction row's `text` was always
  present; `GET /instructions/{id}` returned it).
- After the fix the page loads a single `prosemirror-state` copy and the editor
  mounts on every navigation pattern tried (fresh load, SPA route push, cold
  Vite cache, warm cache).
- Pre-existing, unrelated: a `net::ERR_CONNECTION_RESET` console error for a
  third-party resource appears in dev with or without the fix; the
  `MonacoDiffEditor` duplicate-import WARN in the dev log likewise predates
  this change.
