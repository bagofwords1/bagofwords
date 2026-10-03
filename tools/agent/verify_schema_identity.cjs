// Run from the repository root with a local Nuxt dev server at SCHEMA_PREVIEW_URL.
// Synthetic API responses only; no provider credentials or customer data.
const { chromium, expect } = require('../../frontend/node_modules/@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const preview = path.join(root, 'frontend/pages/users/schema-preview.vue');
const evidence = path.join(root, 'media/pr/powerbi-schema-identity');
const url = process.env.SCHEMA_PREVIEW_URL || 'http://localhost:3100/users/schema-preview';
const fixture = `<template><div class="min-h-screen bg-gray-50 p-12"><div class="mx-auto max-w-5xl rounded-xl border bg-white p-6"><h1 class="text-lg font-semibold mb-1">Sales analytics</h1><p class="text-sm text-gray-500 mb-6">Tables available to this agent</p><TablesSelector ds-id="preview-agent" schema="full" :show-stats="false" /></div></div></template>
<script setup lang="ts">
import TablesSelector from '~/components/datasources/TablesSelector.vue'
definePageMeta({layout:false,auth:false})
usePermissions().value = useRoute().query.reader === '1' ? [] : ['manage_connections']
usePermissionsLoaded().value = true
</script>`;

(async () => {
  if (fs.existsSync(preview)) throw new Error('Preview page already exists.');
  fs.writeFileSync(preview, fixture);
  fs.mkdirSync(evidence, { recursive: true });
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    for (const scenario of ['personal', 'mixed', 'shared', 'partial', 'failed', 'disconnected', 'reader', 'he']) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, recordVideo: scenario === 'mixed' ? { dir: path.join(evidence, 'video'), size: { width: 1280, height: 900 } } : undefined });
      if (scenario === 'he') await context.addInitScript(() => localStorage.setItem('bow.locale', 'he'));
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      const mixed = scenario === 'mixed';
      const onlyShared = scenario === 'shared';
      let identity = scenario === 'disconnected' ? 'none' : 'user';
      const pbi = () => ({ id: 'demo-pbi', name: 'Sales semantic model', type: scenario === 'personal' ? 'ms_fabric' : 'powerbi', data_shape: 'tables', catalog_ownership: 'shared', auth_policy: 'user_required', user_status: { effective_auth: identity, has_user_credentials: identity === 'user', can_switch_identity: scenario !== 'reader' && identity !== 'none', query_identity: identity === 'system' ? 'service_account' : 'self' } });
      const warehouse = { id: 'demo-sql', name: 'Warehouse', type: 'postgresql', data_shape: 'tables', catalog_ownership: 'shared', auth_policy: 'system_only', user_status: null };
      const connections = () => onlyShared ? [warehouse] : mixed ? [pbi(), warehouse] : [pbi()];
      let reads = 0, posts = [], target = '', polls = 0;
      await page.route('**/api/**', async route => {
        const request = route.request();
        const u = new URL(request.url());
        if (!u.pathname.startsWith('/api/')) return route.continue();
        let data = {};
        const org = { id: 'demo-org', name: 'Demo workspace', role: 'admin', permissions: ['manage_connections'] };
        if (u.pathname.includes('organizations')) data = [org];
        if (u.pathname.includes('whoami')) data = { id: 'demo-user', email: 'demo@example.test', organizations: [org] };
        if (u.pathname.endsWith('/connections')) data = connections();
        if (u.pathname.endsWith('/connections/demo-pbi')) data = pbi();
        if (u.pathname.endsWith('/accessible-agents')) data = [];
        if (u.pathname.endsWith('/query-identity')) {
          identity = request.postDataJSON().query_identity === 'self' ? 'user' : 'system';
          data = pbi().user_status;
        }
        if (u.pathname.includes('full_schema')) {
          reads++;
          data = { tables: ['Orders', 'Customers', 'Products'].map((name, i) => ({ id: String(i), name: 'Sales/' + name, connection_id: onlyShared ? 'demo-sql' : 'demo-pbi', is_active: true, columns: [{ name: i ? 'Name' : 'OrderDate', dtype: 'string' }], fks: [], pks: [], metadata_json: {} })), total: 3, total_tables: 3, page: 1, page_size: 100, total_pages: 1, has_more: false, schemas: [], connections: connections(), selected_count: 3 };
        }
        if (u.pathname.includes('/indexing')) {
          const jobScope = u.searchParams.get('scope');
          if (target === `${u.pathname}?scope=${jobScope}`) polls++;
          const active = target === `${u.pathname}?scope=${jobScope}`;
          data = { id: 'demo-job', status: active && polls < 2 ? 'running' : active && scenario === 'failed' ? 'failed' : 'completed', finished_at: '2026-09-22T12:20:00Z', scope: jobScope, stats: { table_count: 3, ...(active && scenario === 'partial' ? { unreadable_dataset_count: 1 } : {}) }, events: [] };
        }
        if (request.method() === 'POST' && u.pathname.includes('/connections/')) {
          posts.push(u.pathname + u.search);
          const jobScope = u.pathname.endsWith('/refresh') && !u.pathname.includes('/my-schema/') ? 'org' : 'user';
          target = `/api/connections/${u.pathname.split('/')[3]}/indexing?scope=${jobScope}`;
          polls = 0;
          data = { indexing: { id: 'demo-job', status: 'running', scope: jobScope } };
        }
        await route.fulfill({ json: data });
      });
      await page.goto(scenario === 'reader' ? `${url}?reader=1` : url);
      const strips = page.getByTestId('schema-identity');
      await expect(strips).toHaveCount(mixed ? 2 : 1, { timeout: 60000 });
      await expect(page.getByText('Sales/Orders', { exact: true })).toBeVisible();
      const refresh = page.getByRole('button', { name: 'Refresh schema', exact: true });
      if (scenario === 'disconnected') {
        await expect(strips.first()).toContainText('Connect your account');
        await expect(refresh).toHaveCount(0);
      } else if (scenario === 'he') {
        await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
        await expect(strips.first()).toContainText('החשבון שלי');
        await page.getByRole('button', { name: 'רענון סכימה', exact: true }).click();
        await expect(page.getByRole('menu')).toBeVisible();
        await expect.poll(() => page.getByRole('menu').locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0))).toBe(true);
        await page.screenshot({ path: path.join(evidence, 'after-refresh-menu-he.png') });
      } else {
        await expect(refresh).toBeVisible();
        if (scenario === 'reader') await expect(strips.first().getByRole('button', { name: 'My account', exact: true })).toHaveCount(0);
        const beforeReads = reads;
        await refresh.click();
        if (onlyShared || scenario === 'reader') {
          await expect.poll(() => posts.length).toBe(1); // The sole action runs directly.
        } else {
          expect(posts).toHaveLength(0); // The entire multi-action button opens its menu.
          const menu = page.getByRole('menu');
          await expect(menu).toBeVisible();
          await expect(menu.getByRole('menuitem')).toHaveCount(mixed ? 3 : 2);
          await expect.poll(() => menu.locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0))).toBe(true);
          if (mixed) await page.screenshot({ path: path.join(evidence, 'after-refresh-menu-mixed.png') });
          if (scenario === 'personal') await page.screenshot({ path: path.join(evidence, 'after-refresh-menu-single.png') });
          await menu.getByRole('menuitem', { name: onlyShared ? 'Warehouse' : 'Sales semantic model · My account' }).click();
          if (mixed) {
            await expect(page.getByTestId('schema-refresh-spinner')).toBeVisible();
            await page.screenshot({ path: path.join(evidence, 'after-refresh-loading.png') });
          }
        }
        await expect(strips.first()).toContainText(scenario === 'partial' ? 'Some models could not be refreshed' : scenario === 'failed' ? 'Schema refresh did not complete' : 'Schema updated.', { timeout: 15000 });
        expect(posts).toEqual([onlyShared ? '/api/connections/demo-sql/refresh' : '/api/connections/demo-pbi/my-schema/refresh?background=true']);
        await expect.poll(() => reads).toBeGreaterThan(beforeReads);
        if (mixed) {
          await refresh.click();
          const menu = page.getByRole('menu');
          await menu.getByRole('menuitem', { name: 'Sales semantic model · Shared schema' }).click();
          await expect.poll(() => posts.length).toBe(2);
          expect(posts[1]).toBe('/api/connections/demo-pbi/refresh');
          await expect(strips.first()).toContainText('Shared schema');
        }
        if (scenario === 'personal') {
          await strips.first().getByRole('button', { name: 'My account', exact: true }).click();
          await expect(page.getByRole('radio')).toHaveCount(2);
          await page.getByRole('radio').nth(1).check();
          await expect(strips.first()).toContainText('Service account');
        }
        if (scenario === 'shared') await page.screenshot({ path: path.join(evidence, 'after-refresh-direct.png') });
        if (scenario === 'partial') await page.screenshot({ path: path.join(evidence, 'after-refresh-partial.png') });
        if (scenario === 'failed') await page.screenshot({ path: path.join(evidence, 'after-refresh-failed.png') });
      }
      expect(errors).toEqual([]);
      await context.close();
      console.log(`PASS ${scenario}`);
    }
  } finally {
    await browser?.close();
    fs.unlinkSync(preview);
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
