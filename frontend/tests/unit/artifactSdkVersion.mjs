// artifact-sdk.js is cached by its ?v= token. A content change without a bump
// would leave browsers on the old SDK, so the pin holds both the token (in every
// place that loads the SDK) and the SHA-256 of the file.
//
//   cd frontend && node tests/unit/artifactSdkVersion.mjs
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const SDK_VERSION = '6';
const SDK_SHA256 = 'ce4a446c14260a61008cf2c172387dd588bc0f1c3a0fd5b14734b7785920d67e';
const BUMP = 'artifact-sdk.js changed: bump v= in utils/artifactIframe.ts and update SDK_SHA256 and the v= pin here';

const frontend = new URL('../../', import.meta.url);
const repo = new URL('../', frontend);
const read = (u) => fs.readFileSync(u, 'utf8');

// 1. The page iframe loads the SDK with the pinned token.
const iframe = read(new URL('utils/artifactIframe.ts', frontend));
assert.ok(iframe.includes(`libUrl('artifact-sdk.js', 'v=${SDK_VERSION}')`), `artifactIframe.ts loads artifact-sdk.js with v=${SDK_VERSION}`);
console.log(`PASS: artifactIframe.ts loads artifact-sdk.js?v=${SDK_VERSION}`);

// 2. No other SDK URL anywhere carries a different token (MCP shells, backend libs, components).
const SDK_URL = /artifact-sdk\.js(?:\?|['"`],\s*['"`])v=(\d+)/g;
const roots = ['frontend/utils', 'frontend/components', 'frontend/composables', 'frontend/pages', 'frontend/plugins', 'frontend/server', 'frontend/public', 'backend/app'];
const skip = new Set(['node_modules', '.output', '.nuxt', '__pycache__', 'libs']);
const found = [];
function walk(dir) {
  if (!fs.existsSync(dir)) return;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) { if (!skip.has(entry.name)) walk(p); continue; }
    if (!/\.(ts|js|mjs|vue|html|py)$/.test(entry.name)) continue;
    for (const m of read(p).matchAll(SDK_URL)) found.push([path.relative(repo.pathname, p), m[1]]);
  }
}
for (const r of roots) walk(new URL(r, repo).pathname);
assert.ok(found.some(([f]) => f === 'frontend/utils/artifactIframe.ts'), 'the scan sees the artifactIframe.ts reference');
assert.deepEqual(found.filter(([, v]) => v !== SDK_VERSION), [], `every artifact-sdk.js URL carries v=${SDK_VERSION}`);
console.log(`PASS: every artifact-sdk.js URL in the repo carries v=${SDK_VERSION} (${found.map(([f]) => f).join(', ')})`);

// 3. The SDK content matches the pinned hash: a change needs a bump.
function sdkHash(text) { return crypto.createHash('sha256').update(text).digest('hex'); }
const sdkFile = process.env.ARTIFACT_SDK_FILE || new URL('public/libs/artifact-sdk.js', frontend).pathname;
assert.equal(sdkHash(fs.readFileSync(sdkFile)), SDK_SHA256, BUMP);
console.log('PASS: artifact-sdk.js matches SDK_SHA256');
