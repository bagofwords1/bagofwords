// Verify mermaid rendering in the owner's doc EDITOR (TipTap) — the default
// view a report owner gets for a mode='doc' artifact (ArtifactFrame opens
// DocEditor, not the read-only DocViewer).
//
// Checks, against a booted stack (tools/agent/boot_stack.sh --dev):
//   1. a ```mermaid fence renders as an SVG diagram (source hidden)
//   2. clicking the diagram reveals the editable source
//   3. edits update the diagram; leaving the block hides the source again
//   4. Save round-trips the ```mermaid fence + edit into the new doc version
//
// Setup: a report owned by the login user, with a mode='doc' artifact whose
// markdown contains a ```mermaid fence (see docs/feedback-loops/doc-artifacts.md).
//
// Run from frontend/ so @playwright/test resolves:
//   cd frontend
//   BOW_EMAIL=admin@example.com BOW_PASSWORD='Password123!' \
//     node ../tools/agent/verify_doc_editor_mermaid.mjs <reportId>
import { chromium } from '@playwright/test';

const BASE = process.env.BOW_BASE_URL || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const OUT_DIR = process.env.OUT_DIR || '.';
const reportId = process.argv[2];
if (!reportId) { console.error('usage: node verify_doc_editor_mermaid.mjs <reportId>'); process.exit(2); }

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

// UI login (skip onboarding if it intercepts)
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.getByPlaceholder('Email').fill(EMAIL);
await page.getByPlaceholder('Password').fill(PASSWORD);
await page.locator('button[type=submit]').click();
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 30000 });
if (page.url().includes('/onboarding')) {
  const skip = page.getByText('Skip onboarding', { exact: true });
  await skip.waitFor({ state: 'visible', timeout: 15000 });
  await skip.click();
  await page.waitForURL((u) => !u.pathname.includes('/onboarding'), { timeout: 15000 }).catch(() => {});
}

// The owner's doc opens in the TipTap editor by default
await page.goto(`${BASE}/reports/${reportId}`, { waitUntil: 'domcontentloaded' });
await page.waitForSelector('.bow-doc-editor .ProseMirror', { timeout: 45000 });

// 1. Diagram renders, source hidden
await page.waitForSelector('.doc-codeblock-node .doc-mermaid svg', { timeout: 30000 });
const sourceVisibleBefore = await page.locator('.doc-codeblock-node pre').first().isVisible();
console.log('diagram svg rendered; source visible before click:', sourceVisibleBefore);
await page.screenshot({ path: `${OUT_DIR}/doc-editor-mermaid-rendered.png` });

// 2. Click → editable source appears
await page.locator('.doc-mermaid-preview').first().click();
await page.waitForSelector('.doc-codeblock-node pre:visible', { timeout: 5000 });
await page.locator('.doc-codeblock-node pre').first().scrollIntoViewIfNeeded();
console.log('source shown after click:', await page.locator('.doc-codeblock-node pre').first().isVisible());
await page.screenshot({ path: `${OUT_DIR}/doc-editor-mermaid-source.png` });

// 3. Edit the source, then click into prose → source hides, diagram updates
await page.keyboard.press('End');
await page.keyboard.type('\nWave --> Impact[Margin impact]');
await page.waitForTimeout(700); // preview debounce
await page.locator('.bow-doc-editor h1, .bow-doc-editor h2, .bow-doc-editor p').first().click();
await page.waitForTimeout(400);
const sourceHidden = !(await page.locator('.doc-codeblock-node pre').first().isVisible());
const svgText = await page.locator('.doc-codeblock-node .doc-mermaid svg').first().textContent();
console.log('source hidden after leaving:', sourceHidden, '| diagram contains new node:', svgText.includes('Margin impact'));
await page.screenshot({ path: `${OUT_DIR}/doc-editor-mermaid-after-edit.png` });

// 4. Save → the ```mermaid fence and the edit survive the round-trip
const [saveResp] = await Promise.all([
  page.waitForResponse((r) => r.url().includes('/doc_edit') && r.request().method() === 'POST', { timeout: 20000 }),
  page.getByRole('button', { name: /save/i }).click(),
]);
const md = (await saveResp.json())?.content?.markdown || '';
console.log('saved status:', saveResp.status(), '| fence preserved:', md.includes('```mermaid'), '| edit kept:', md.includes('Margin impact'));

await browser.close();

const ok = !sourceVisibleBefore && sourceHidden && svgText.includes('Margin impact')
  && saveResp.status() === 200 && md.includes('```mermaid') && md.includes('Margin impact');
console.log(ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
