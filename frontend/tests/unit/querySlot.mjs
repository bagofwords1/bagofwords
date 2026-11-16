// The chat's "waiting for a free slot" label. A query waits behind running
// queries AND earlier queued ones, so "1/2 running" alone would read as a
// free slot; the label must name the queue ahead of it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../../utils/querySlot.ts', import.meta.url), 'utf8');
const sandbox = { exports: {} };
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, sandbox);
const { slotWaitText } = sandbox.exports;
const t = (key, params) => `${key}${params ? ' ' + JSON.stringify(params) : ''}`;

assert.equal(slotWaitText(t, null), '');
assert.equal(
  slotWaitText(t, { connection_name: 'pg', running: 2, limit: 2, queued_ahead: 0 }),
  'tools.common.waitingForSlot {"connection":"pg","running":2,"limit":2}',
);
assert.equal(
  slotWaitText(t, { connection_name: 'pg', running: 1, limit: 2, queued_ahead: 1 }),
  'tools.common.waitingForSlotAhead {"connection":"pg","running":1,"limit":2,"ahead":1}',
);
assert.equal(
  slotWaitText(t, { connection_name: null }),
  'tools.common.waitingForSlotShort {"connection":"tools.common.theConnection"}',
);
console.log('PASS: the slot wait label names the queue ahead when there is one');
