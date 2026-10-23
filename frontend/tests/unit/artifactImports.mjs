// Real Chromium: import statements in artifact code.
//
// Artifact code runs as a classic script, so artifact-globals removes import
// statements before it executes. Imports that introduce a NEW local name
// (aliases, default/namespace imports under another name) must be bound to the
// sandbox globals instead — removing them left the name undefined and the
// dashboard crashed ("useLocal is not defined"). Plain imports of names that
// already exist (useEffect, useMemo) keep being removed.
//
//   cd frontend && node tests/unit/artifactImports.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';

const root = new URL('../../', import.meta.url);
const source = fs.readFileSync(new URL('utils/artifactIframe.ts', root), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
if (!fs.existsSync(new URL('public/libs/babel-standalone.min.js', root))) {
  console.log('SKIP: vendored libs missing (scripts/download-vendor-libs.sh)');
  process.exit(0);
}

const CODE = `<script type="text/babel">
import React, { useState as useLocal, useMemo } from 'react';
import R from 'react';
import * as E from 'echarts';
import {
  createRoot as makeRoot,
  default as RD
} from 'react-dom/client';
import { Thing as Missing } from 'some-unknown-lib';
import './styles.css';
import { useEffect } from 'react';

const Card = ({ children }) => <section id="card">{children}</section>;

function App() {
  const [n, setN] = useLocal(1);
  const doubled = useMemo(() => n * 2, [n]);
  useEffect(() => { setN(5); }, []);
  return (
    <Card>
      <p id="n">{doubled}</p>
      <p id="same">{String(R === React)}</p>
      <p id="echarts">{typeof E.init}</p>
      <p id="dom">{typeof makeRoot}/{String(RD === ReactDOM)}</p>
      <p id="missing">{typeof Missing}</p>
    </Card>
  );
}
makeRoot(document.getElementById('root')).render(<App />);
</script>`;

const browser = await chromium.launch({headless: true});
try {
  const page = await browser.newPage();
  await page.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.pathname.startsWith('/libs/')) {
      const file = new URL('public' + u.pathname, root);
      if (fs.existsSync(file)) return route.fulfill({path: file.pathname, headers: {'Access-Control-Allow-Origin': '*'}});
    }
    return route.fulfill({status: 200, body: '<html><body></body></html>', contentType: 'text/html'});
  });
  await page.goto('http://artifact-imports.test/');
  await page.addScriptTag({content: 'window.exports={};' + compiled});
  await page.evaluate((code) => {
    window.__msgs = [];
    window.addEventListener('message', (e) => { if (e.data && typeof e.data.type === 'string') window.__msgs.push(e.data.type + (e.data.payload && e.data.payload.message ? ': ' + e.data.payload.message : '')); });
    const f = document.createElement('iframe');
    f.sandbox = 'allow-scripts';
    f.srcdoc = exports.buildArtifactIframeHtml({data: {visualizations: []}, code});
    document.body.appendChild(f);
  }, CODE);
  await page.waitForFunction(() => window.__msgs.some((m) => m.startsWith('ARTIFACT_READY') || m.startsWith('ARTIFACT_ERROR')), null, {timeout: 20000});
  const msgs = await page.evaluate(() => window.__msgs.filter((m) => m.startsWith('ARTIFACT_')));
  assert.deepEqual(msgs, ['ARTIFACT_READY'], `rendered without errors: ${msgs}`);
  const frame = page.frames().find((f) => f.parentFrame());
  const text = (id) => frame.evaluate((i) => document.getElementById(i)?.textContent, id);
  assert.equal(await text('n'), '10', 'aliased useState + plain useMemo/useEffect work');
  assert.equal(await text('same'), 'true', 'default import under another name is React');
  assert.equal(await text('echarts'), 'function', 'namespace import binds to the echarts global');
  assert.equal(await text('dom'), 'function/true', 'multi-line aliased + default-as imports bind to ReactDOM');
  assert.equal(await text('missing'), 'undefined', 'an alias from an unknown module is undefined, not a crash');
  assert.ok(await frame.$('#card'), 'the component tree rendered');
  console.log('artifactImports: OK');
} finally {
  await browser.close();
}
