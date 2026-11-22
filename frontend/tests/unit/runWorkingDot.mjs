// The run-activity dot on /reports/:id stays on for every live frame,
// whatever the newest block holds, and goes away only when the run ends,
// is being stopped, or waits on the user.
//   cd frontend && node tests/unit/runWorkingDot.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../../composables/runWorkingDot.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText;
const {isRunWorking} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

const live = {role: 'system', status: 'in_progress'};
const awaiting = (b) => !!b.awaiting;
const text = {content: 'Let me check the Invoice table.'};
const runningTool = {tool_execution: {status: 'running', tool_name: 'create_data'}};
const reasoning = {reasoning: 'thinking…'};

// Every live shape the old rules hid the dot in.
assert.equal(isRunWorking(live, [], awaiting), true, 'before the first event');
assert.equal(isRunWorking(live, [reasoning], awaiting), true, 'while reasoning streams');
assert.equal(isRunWorking(live, [text], awaiting), true, 'after text, waiting on the next round');
assert.equal(isRunWorking(live, [text, runningTool], awaiting), true, 'while a visible tool runs');
assert.equal(isRunWorking(live, [runningTool, text], awaiting), true, 'while the answer streams');

// When it goes away.
assert.equal(isRunWorking({...live, status: 'success'}, [text], awaiting), false, 'finished');
assert.equal(isRunWorking({...live, status: 'error'}, [], awaiting), false, 'failed');
assert.equal(isRunWorking({...live, status: 'stopped'}, [], awaiting), false, 'stopped');
assert.equal(isRunWorking({...live, sigkill: '2026-11-20T10:00:00'}, [text], awaiting), false, 'being stopped');
assert.equal(isRunWorking(live, [text, {awaiting: true}], awaiting), false, 'waiting on a confirmation');
assert.equal(isRunWorking({role: 'user', status: 'in_progress'}, [], awaiting), false, 'user bubbles never show it');
console.log('PASS: run-activity dot stays on for every live frame and leaves on end/stop/await');
