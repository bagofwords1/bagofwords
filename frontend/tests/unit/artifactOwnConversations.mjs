// An artifact that holds its own chat (bow.conversations, or the
// useConversation building block) hides the shared page's chat bubble; and the
// globals URL moves its revision= token for the chat building blocks while the
// runtime version (it decides the legacy vs themed look) stays put.
//
//   cd frontend && node tests/unit/artifactOwnConversations.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';

const root = new URL('../../', import.meta.url);
const read = (p) => fs.readFileSync(new URL(p, root), 'utf8');

const view = read('components/dashboard/SharedDashboardView.vue');
const line = view.split('\n').find((l) => /^const ownConversations\b/.test(l));
assert.ok(line, 'SharedDashboardView.vue defines ownConversations');
const literal = /\/((?:\\\/|[^/])+)\/([a-z]*)\.test\(/.exec(line);
assert.ok(literal, 'ownConversations tests the code with a regex literal');
const own = new RegExp(literal[1], literal[2]);
for (const code of ['for await (const e of bow.conversations.send(id, text)) {}', 'const chat = useConversation();', 'const chat=useConversation({title});', 'useConversation ({agents})']) {
  assert.equal(own.test(code), true, `the app is the chat: ${code}`);
}
for (const code of ['<ChatResult result={r}/>', 'Start a conversation', 'const conversation = null;', 'useConversations()']) {
  assert.equal(own.test(code), false, `not a chat: ${code}`);
}
console.log('PASS: the page bubble hides for bow.conversations and useConversation( apps only');

const iframe = read('utils/artifactIframe.ts');
const shell = read('public/mcp-artifact-app.html');
assert.match(iframe, /export const ARTIFACT_GLOBALS_VERSION = '11';/, 'the runtime version is unchanged');
assert.match(iframe, /libUrl\('artifact-globals\.js', `v=\$\{ARTIFACT_GLOBALS_VERSION\}&revision=chat-ended-1`\)/);
assert.match(shell, /'\/libs\/artifact-globals\.js\?v=11&revision=chat-ended-1'/);
assert.doesNotMatch(iframe, /Bump this whenever/, 'the stale "bump the version" advice is gone');
const versionDoc = iframe.slice(iframe.lastIndexOf('/**', iframe.indexOf('export const ARTIFACT_GLOBALS_VERSION')), iframe.indexOf('export const ARTIFACT_GLOBALS_VERSION'));
assert.match(versionDoc, /revision=/, 'the version comment points at the revision token');
assert.match(shell, /<!--[^>]*revision=[^>]*-->/, 'the MCP shell comment mentions the revision token');
console.log('PASS: the globals URLs carry revision=chat-ended-1 and ARTIFACT_GLOBALS_VERSION stays 11');
