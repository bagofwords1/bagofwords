import { test, expect } from '../fixtures/feature-test';

// Regression: the instruction detail pane must render the instruction text.
//
// It once rendered completely blank (title + metadata visible, body empty)
// because two copies of prosemirror-state were loaded in dev — the tiptap
// packages registered by nuxt-tiptap-editor are excluded from Vite
// pre-bundling while the app's own @tiptap/extension-mention was pre-bundled
// with an inlined second copy. The two copies' auto-generated plugin keys
// collide and Editor creation aborts with "RangeError: Adding different
// instances of a keyed plugin (plugin$…)" before EditorContent mounts.
// See docs/feedback-loops/instructions-empty-editor.md.
//
// The assertions are general invariants: the editor mounts with the text
// visible, and no keyed-plugin error fires anywhere on the page.
test('instruction detail renders its text in the editor', async ({ page }) => {
  const keyedPluginErrors: string[] = [];
  page.on('pageerror', (e) => {
    if (String(e).includes('keyed plugin')) keyedPluginErrors.push(String(e));
  });
  page.on('console', (m) => {
    if (m.type() === 'error' && m.text().includes('keyed plugin')) keyedPluginErrors.push(m.text());
  });

  // Seed an instruction through the app's API, reusing the session's auth
  // cookie (raw JWT; the app sends it as a Bearer header). The cookie name
  // depends on the nuxt-auth version ('auth.token' today, 'auth_token' per
  // nuxt.config) — accept either.
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;

  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  expect(whoami.ok()).toBeTruthy();
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  expect(orgId, 'admin user should belong to an organization').toBeTruthy();

  const marker = `editor-render-check-${Date.now()}`;
  const created = await page.request.post('/api/instructions', {
    headers: { Authorization: auth, 'X-Organization-Id': orgId },
    data: {
      title: 'Editor Render Regression Instruction',
      text: `Always render this body. Unique marker: ${marker}.`,
      status: 'published',
      category: 'general',
      load_mode: 'always',
      data_source_ids: [],
    },
  });
  expect(created.ok(), `create instruction failed: ${created.status()}`).toBeTruthy();
  const instruction = await created.json();

  // Open the instruction detail directly (the Knowledge Explorer deep link).
  await page.goto(`/agents/instructions/${instruction.id}`, { waitUntil: 'commit' });

  // Title renders even when the editor crashes — wait for it first.
  await expect(page.getByText('Editor Render Regression Instruction').first())
    .toBeVisible({ timeout: 45000 });

  // The actual regression: the tiptap editor must mount and show the body.
  await expect(page.locator('.tiptap-prose').first())
    .toContainText(marker, { timeout: 30000 });

  expect(keyedPluginErrors, `keyed-plugin errors:\n${keyedPluginErrors.join('\n')}`).toEqual([]);
});
