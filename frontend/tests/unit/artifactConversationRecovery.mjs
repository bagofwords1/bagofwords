// Public hook/component behavior in the real artifact iframe; only host I/O is controlled.
import assert from 'node:assert/strict';
import {start, run, sendStarted, pageOf, a, u, result} from './support/chatHarness.mjs';

const scenarios = [];
for (const lateStarted of [false, true]) scenarios.push({
  name: `failed live Stop restores a retryable answer (late started: ${lateStarted})`,
  fn: async env => {
    const s = await start(env, {auto: {stop: 'hold', messages: pageOf([u('question', 'Count orders'), a('answer', 'Working', {status: 'in_progress'})])}});
    const req = await sendStarted(s, 'Count orders', lateStarted ? null : 'answer');
    await s.call('stop');
    if (lateStarted) await s.event(req.id, {type: 'started', message_id: 'answer'});
    const stop = await s.take('stop');
    await s.fail(stop.id, {code: 'UNAVAILABLE', message: 'Stop did not reach the server'});
    await s.until(c => c.answering && c.error?.code === 'UNAVAILABLE', 'recoverable stop failure');
    await s.call('stop');
    const retry = await s.take('stop');
    assert.equal(retry.args.messageId, 'answer');
    await s.reply(retry.id, {id: 'answer', status: 'stopped'});
    await s.until(c => !c.answering, 'retry stops the answer');
    await s.done();
  }
});

scenarios.push({name: 'a late Stop rejection cannot change another conversation', fn: async env => {
  const s = await start(env, {auto: {stop: 'hold'}});
  await sendStarted(s, 'First question', 'old-answer');
  await s.call('stop');
  const stop = await s.take('stop');
  await s.call('newConversation');
  await s.fail(stop.id, {code: 'UNAVAILABLE', message: 'Late failure'});
  await s.call('reload');
  const state = await s.chat();
  assert.equal(state.error, null);
  assert.equal(state.answering, false);
  assert.deepEqual(state.messages, []);
  await s.done();
}});

for (const reopened of [false, true]) scenarios.push({
  name: `Stop stays retryable when both Stop and history fail (reopened: ${reopened})`,
  fn: async env => {
    const s = await start(env, {opts: reopened ? {conversationId: 'open'} : {}, auto: {
      stop: 'hold', messages: pageOf(reopened ? [a('answer', 'Working', {status: 'in_progress'})] : [])
    }});
    if (!reopened) await sendStarted(s, 'Count orders', 'answer');
    await env.page.evaluate(() => { window.__host.auto.messages = {__error: {code: 'UNAVAILABLE', message: 'Offline'}}; });
    await s.call('stop');
    const stop = await s.take('stop');
    await s.fail(stop.id, {code: 'UNAVAILABLE', message: 'Offline'});
    await s.until(c => c.answering && c.error?.code === 'UNAVAILABLE', 'Stop recovery while offline');
    await s.call('stop');
    const retry = await s.take('stop');
    assert.equal(retry.args.messageId, 'answer');
    await s.reply(retry.id, {id: 'answer', status: 'stopped'});
    await s.until(c => !c.answering, 'retry succeeds');
    await s.done();
  }
});

for (const list of [false, true]) scenarios.push({
  name: `transient ${list ? 'thread list' : 'history'} failures retry and observe completion`, clock: true,
  fn: async env => {
    const running = a('running-answer', 'Working', {status: 'in_progress'});
    const s = await start(env, {opts: {conversationId: 'open'}, auto: {
      messages: pageOf(list ? [] : [running]),
      list: {items: list ? [{id: 'other', title: 'Other', in_progress: true}] : []}
    }});
    const op = list ? 'list' : 'messages';
    await s.take(op);
    await env.page.evaluate(op => { window.__host.auto[op] = 'hold'; }, op);
    await env.page.clock.runFor(5001);
    const failed = await s.take(op);
    await s.fail(failed.id, {code: 'UNAVAILABLE', message: 'Transient failure'});
    // A round-trip through the frame lets the rejection handler settle before advancing time.
    await s.dom(() => Promise.resolve());
    await env.page.clock.runFor(5001);
    const retry = await s.take(op);
    await s.reply(retry.id, list
      ? {items: [{id: 'other', title: 'Other', in_progress: false}]}
      : pageOf([a('running-answer', 'Finished')]));
    await s.until(c => list ? !c.conversations.some(x => x.in_progress) : !c.answering, 'completion after retry');
    await s.done();
  }
});

scenarios.push({name: 'refresh replaces changed result content and keeps unchanged result objects', fn: async env => {
  const original = result('sales', {chart: {type: 'bar', x: 'm', y: ['v'], series: null}});
  const s = await start(env, {opts: {conversationId: 'open'}, auto: {messages: pageOf([a('answer', 'Sales', {results: [original]})])}});
  for (const patch of [
    {rows: [{m: '[REDACTED]', v: 7}, {m: '[REDACTED]', v: 9}]},
    {columns: [{field: 'm', headerName: 'Period', type: 'string'}, {field: 'v', headerName: 'Total', type: 'number'}]},
    {title: 'Updated title'}, {chart: {type: 'line', x: 'm', y: ['v'], series: null}},
    {row_count: 40, truncated: true}
  ]) {
    const fresh = {...original, ...patch};
    const version = (await s.chat()).version;
    await env.page.evaluate(value => { window.__host.auto.messages = value; }, pageOf([a('answer', 'Sales', {results: [fresh]})]));
    await s.call('reload');
    await s.until(c => c.version > version, 'refreshed result');
    assert.deepEqual((await s.chat()).messages[0].results[0], fresh);
  }
  await s.dom(() => { window.__lastResult = window.__chat.messages[0].results[0]; });
  const version = (await s.chat()).version;
  await s.call('reload');
  await s.until(c => c.version > version, 'unchanged refresh');
  assert.equal(await s.dom(() => window.__lastResult === window.__chat.messages[0].results[0]), true);
  await s.done();
}});

scenarios.push({name: 'direct send is ignored while a reopened server answer is running', fn: async env => {
  const s = await start(env, {opts: {conversationId: 'open'}, auto: {messages: pageOf([a('answer', 'Working', {status: 'in_progress'})])}});
  const before = (await s.chat()).messages;
  await s.call('send', 'Must wait');
  assert.equal(await s.count('send'), 0);
  assert.deepEqual((await s.chat()).messages, before);
  assert.equal((await s.chat()).error, null);
  await s.done();
}});

scenarios.push({name: 'a send queued during history loading cannot overlap the server answer', fn: async env => {
  const s = await start(env, {opts: {conversationId: 'open'}, waitLoad: false, auto: {messages: 'hold'}});
  const load = await s.take('messages');
  await s.call('send', 'Must wait');
  await s.reply(load.id, pageOf([a('answer', 'Working', {status: 'in_progress'})]));
  await s.until(c => !c.loading && !c.sending, 'loaded running answer');
  assert.equal(await s.count('send'), 0);
  assert.equal((await s.chat()).messages.length, 1);
  await s.done();
}});

for (const component of ['ChatMessage', 'ChatMessages']) scenarios.push({
  name: `${component} can hide result tables without losing Markdown or earlier steps`,
  fn: async env => {
    const message = a('answer', '**Final answer**', {steps: ['Earlier **analysis**'], results: [result('sales')], results_incomplete: true});
    const code = `<script>
      function App() {
        return React.createElement(${component}, {
          message: ${JSON.stringify(message)}, chat: {messages: [${JSON.stringify(message)}]}, showResults: false
        });
      }
      ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
    </script>`;
    const s = await start(env, {code, waitLoad: false});
    assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-result"]').length), 0);
    assert.equal(await s.dom(() => document.querySelectorAll('[data-testid="bow-chat-incomplete"]').length), 0);
    assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-text"] strong')?.textContent), 'Final answer');
    assert.equal(await s.dom(() => document.querySelector('[data-testid="bow-chat-step"] strong')?.textContent), 'analysis');
    await s.done();
  }
});

await run('artifactConversationRecovery', scenarios);
