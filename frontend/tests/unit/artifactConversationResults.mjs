// bow.conversations results: the verifier fixture answers with two sample
// `result`s (and stores them on the message), and the live transport passes a
// host `result` event through unchanged.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const SDK = fs.readFileSync(new URL('../../public/libs/artifact-sdk.js', import.meta.url), 'utf8');
const plain = (v) => JSON.parse(JSON.stringify(v));

function load(windowExtras) {
  const handlers = [];
  const parent = { postMessage() {} };
  const context = { console, Map, Promise, Error, AbortController, setTimeout, clearTimeout };
  context.window = { ARTIFACT_DATA: {}, __artifactDataListeners: [], parent,
    addEventListener(type, fn) { if (type === 'message') handlers.push(fn); }, ...windowExtras };
  vm.runInNewContext(SDK, context);
  return { bow: context.window.bow, handlers, parent };
}

// Two results per answer: a chart, and a cut table without one (rows and columns
// beyond what is shown), so verifier runs reach the table-only and truncation branches.
// Ids are per message (the started event's message_id), unique across answers.
const expected = (mid) => [{
  id: `${mid}-result-1`, block: 'b1', title: 'Preview result',
  columns: [{ field: 'category', headerName: 'Category', type: 'string' },
            { field: 'value', headerName: 'Value', type: 'number' }],
  rows: [{ category: 'A', value: 3 }, { category: 'B', value: 5 }, { category: 'C', value: 2 }],
  row_count: 3, column_count: 2, truncated: false,
  chart: { type: 'bar', x: 'category', y: ['value'], series: null },
}, {
  id: `${mid}-result-2`, block: 'b1', title: 'Preview detail (partial)',
  columns: [{ field: 'name', headerName: 'Name', type: 'string' },
            { field: 'amount', headerName: 'Amount', type: 'number' }],
  rows: [{ name: 'Alpha', amount: 120.5 }, { name: 'Beta', amount: 98 }],
  row_count: 120, column_count: 5, truncated: true, chart: null,
}];

async function sendAll(bow, id, text) {
  const events = [];
  for await (const e of bow.conversations.send(id, text)) events.push(e);
  return events;
}

// S12: fixture mode.
{
  const { bow } = load({ __BOW_FIXTURE_MODE__: true });
  const conv = await bow.conversations.create({ title: 'Preview' });
  const events = (await sendAll(bow, conv.id, 'Revenue by month?')).map(plain);
  // Like the server (artifact_conversations.ndjson): the streamed tokens, then
  // the block's full text, which replaces them.
  assert.deepEqual(events.map((e) => e.type), ['started', 'text_delta', 'text', 'result', 'result', 'completed']);
  const delta = events.find((e) => e.type === 'text_delta'), full = events.find((e) => e.type === 'text');
  assert.deepEqual(full, { type: 'text', block: 'b1', text: 'Preview answer. No agent was called.' });
  assert.equal(full.text, delta.text);
  assert.equal(full.block, delta.block);
  // The automatic check (artifact_verification.py) looks for this reply in the page.
  const verification = fs.readFileSync(new URL('../../../backend/app/ai/tools/artifact_verification.py', import.meta.url), 'utf8');
  const reply = /^FIXTURE_CONVERSATION_REPLY: str = "([^"]+)"$/m.exec(verification);
  assert.ok(reply && full.text.includes(reply[1]), 'FIXTURE_CONVERSATION_REPLY is in the fixture text');
  const mid = events[0].message_id;
  const results = events.filter((e) => e.type === 'result').map(({ type, ...r }) => r);
  assert.deepEqual(results, expected(mid));
  assert.ok(results[1].rows.length < results[1].row_count && results[1].columns.length < results[1].column_count);

  const history = await bow.conversations.messages(conv.id);
  const assistant = history.items.filter((m) => m.role === 'assistant');
  assert.equal(assistant.length, 1);
  assert.equal(assistant[0].id, mid);  // the started message_id is the stored message
  assert.deepEqual(plain(assistant[0].results), expected(mid));
  assert.equal(history.items.find((m) => m.role === 'user').results, undefined);

  // Each emission is a fresh copy: an app mutating one result cannot change the next.
  const first = (await sendAll(bow, conv.id, 'Again')).find((e) => e.type === 'result');
  first.rows.push({ category: 'Z', value: 99 });
  first.chart.y.push('x');
  assistant[0].results[0].rows.length = 0;
  const secondEvents = await sendAll(bow, conv.id, 'Once more');
  const secondMid = secondEvents[0].message_id;
  assert.deepEqual(secondEvents.filter((e) => e.type === 'result').map((e) => { const { type, ...r } = plain(e); return r; }),
                   expected(secondMid));
  const again = (await bow.conversations.messages(conv.id)).items.filter((m) => m.role === 'assistant');
  assert.equal(again.length, 3);
  assert.deepEqual(plain(again[0].results), expected(mid));
  assert.notEqual(again[1].results[0], again[2].results[0]);
  const ids = again.flatMap((m) => m.results.map((r) => r.id));
  assert.equal(new Set(ids).size, ids.length);  // unique across messages
  console.log('PASS: fixture send yields started, text_delta, text, two results (chart; cut table without one), completed and messages() carries them');
}

// S13: live mode with a fake port.
{
  const nonce = 'live-nonce';
  const { bow, handlers, parent } = load({ __BOW_RUNTIME_NONCE__: nonce });
  const sent = [];
  const port = { onmessage: null, start() {}, postMessage(m) { sent.push(m); } };
  for (const fn of handlers) fn({ source: parent, data: { type: 'BOW_RUNTIME_PORT', nonce }, ports: [port] });

  const hostResult = {
    type: 'result', id: 'step-9', block: 'blk-2', title: 'Revenue per month',
    columns: [{ field: 'month', headerName: 'Month', type: 'string' },
              { field: 'revenue', headerName: 'Revenue', type: 'number' }],
    rows: [{ month: '2024-01', revenue: 10.5 }, { month: '2024-02', revenue: 12 }],
    row_count: 40, column_count: 2, truncated: true, chart: null,
  };
  const events = [];
  const reading = (async () => { for await (const e of bow.conversations.send('conv-1', 'hi')) events.push(e); })();
  for (let i = 0; i < 20 && !sent.length; i++) await new Promise((r) => setTimeout(r, 0));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].method, 'conversationSend');
  const id = sent[0].id;
  port.onmessage({ data: { id, event: { type: 'started', message_id: 'm1' } } });
  port.onmessage({ data: { id, event: hostResult } });
  port.onmessage({ data: { id, event: { type: 'completed', status: 'success' } } });
  port.onmessage({ data: { id, result: null } });
  await reading;
  assert.deepEqual(events.map((e) => e.type), ['started', 'result', 'completed']);
  assert.equal(events[1], hostResult);
  assert.deepEqual(plain(events[1]), plain(hostResult));
  console.log('PASS: live send passes a host result event through unchanged');
}
