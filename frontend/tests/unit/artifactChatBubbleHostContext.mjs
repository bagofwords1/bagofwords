// Inside a host application, the page's chat bubble sends the host's current
// context with each message (finding 2), so "this customer" resolves. The
// server caps it and labels it advisory; it grants no access. Outside a host
// application (no hostContext) the request body is exactly what it was.
//
//   cd frontend && node tests/unit/artifactChatBubbleHostContext.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';

const root = new URL('../../', import.meta.url);
const read = (p) => fs.readFileSync(new URL(p, root), 'utf8');

const bubble = read('components/report/ArtifactChatBubble.vue');
const propsLine = bubble.split('\n').find((l) => /^const props = defineProps</.test(l));
assert.ok(propsLine, 'ArtifactChatBubble.vue declares its props on one line');
assert.match(propsLine, /\bhostContext\?: Record<string, any> \| null\b/, 'the bubble takes an optional hostContext prop');

const bodyLine = bubble.split('\n').find((l) => /^\s*body: JSON\.stringify\(\{ prompt: \{ content/.test(l));
assert.ok(bodyLine, 'the send body is built on one line');
const expr = /body: (JSON\.stringify\(.*\)),\s*$/.exec(bodyLine);
assert.ok(expr, 'the body line is a JSON.stringify call');
// Evaluate the exact body expression with and without host context.
const build = new Function('content', 'props', `return ${expr[1]};`);
assert.deepEqual(JSON.parse(build('what did this customer buy?', { hostContext: { customerId: 'C-14' } })),
  { prompt: { content: 'what did this customer buy?', host_context: { customerId: 'C-14' } }, stream: true });
assert.equal(build('hi', { hostContext: null }), JSON.stringify({ prompt: { content: 'hi' }, stream: true }),
  'no host context: the body is byte-identical to before');
assert.equal(build('hi', {}), JSON.stringify({ prompt: { content: 'hi' }, stream: true }));
assert.equal((bubble.match(/host_context/g) || []).length, 1, 'host_context is only ever sent in the message body');
console.log('PASS: the bubble sends host_context with each message only when it has one');

const view = read('components/dashboard/SharedDashboardView.vue');
const start = view.indexOf('<ArtifactChatBubble');
assert.ok(start >= 0, 'SharedDashboardView.vue renders the bubble');
const element = view.slice(start, view.indexOf('/>', start));
assert.match(element, /\n\s*:host-context="props\.hostContext \?\? null"/, 'the page passes its host context to the bubble');
assert.match(element, /v-if="reportLoaded && report\?\.artifact_chat_enabled && api\.chatBase && !embedded && !ownConversations"/,
  'when the bubble shows is unchanged');
console.log('PASS: SharedDashboardView binds :host-context on the bubble');
