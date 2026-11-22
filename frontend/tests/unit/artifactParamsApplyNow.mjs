// Dashboard parameter changes reach the host without a needless wait, and
// without a flood of commits.
//
// window.__paramStore (public/libs/artifact-globals.js) used to wait 250 ms
// after every setParam/setParams before posting ARTIFACT_SET_PARAMS. That wait
// coalesces typing; a pick from a list the declaration itself fixes (options
// or options_source) is one deliberate change, so it goes out at once. Every
// other param (free text, numbers, ids, lists, dates, and params whose options
// were only derived by the host page) keeps the 250 ms debounce.
//
// Discrete changes made in the same tick become one commit (apply(targets) in
// that tick takes them with its targets); a burst of discrete changes sends
// the first at once and the rest as one trailing commit 250 ms after the last
// commit; a commit that would not change any value is skipped.
//
// The runtime runs in a node vm with inert stand-ins for React, the DOM and
// echarts; timers are fake so the 250 ms is measured, not slept.
//
//   cd frontend && node tests/unit/artifactParamsApplyNow.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const root = new URL('../../', import.meta.url);
const source = fs.readFileSync(new URL('public/libs/artifact-globals.js', root), 'utf8');

// Anything-goes stand-in: every property is another stand-in, every call and
// `new` returns one. Enough for the runtime's top-level setup to run.
function inert() {
  const fn = function () {};
  return new Proxy(fn, {
    get(_t, key) {
      if (key === Symbol.toPrimitive) return () => '';
      if (key === 'then' || key === Symbol.iterator) return undefined;
      if (key === 'length') return 0;
      return inert();
    },
    set() { return true; },
    apply() { return inert(); },
    construct() { return inert(); },
  });
}

const DECLARATIONS = [
  { name: 'region', type: 'string', options: ['North', 'South', 'East', 'West'] },   // fixed list on the declaration
  { name: 'owner', type: 'string', options_source: { query_id: 'q', value_column: 'o' } }, // list from another query
  { name: 'customer', type: 'string' },   // free text; the host page derives options for it from a column
  { name: 'day', type: 'date' },
  { name: 'period', type: 'date_range' },
  { name: 'search', type: 'string' },
  { name: 'limit', type: 'number' },
  { name: 'ids', type: 'list' },
  { name: 'account', type: 'id' },
  { name: 'level', type: 'number', options: Array.from({ length: 31 }, (_, i) => i) },  // a slider over fixed steps
];
const OPTIONS = {
  owner: [{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }],
  customer: [{ value: 'acme', label: 'acme' }, { value: 'globex', label: 'globex' }],
};

const tick = () => new Promise((r) => setImmediate(r));

function boot() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  const posted = [];
  const sandbox = {
    React: inert(), ReactDOM: inert(), echarts: inert(), document: inert(),
    navigator: inert(), localStorage: inert(), getComputedStyle: inert(),
    matchMedia: inert(), requestAnimationFrame: inert(), MutationObserver: inert(),
    ResizeObserver: inert(), Node: inert(), console,
    addEventListener() {}, removeEventListener() {},
    setTimeout(fn, ms) { const id = nextId++; timers.set(id, { fn, at: now + (ms || 0) }); return id; },
    clearTimeout(id) { timers.delete(id); },
    parent: { postMessage(msg) { posted.push({ at: now, msg: JSON.parse(JSON.stringify(msg)) }); } },
    ARTIFACT_DATA: { params: { declarations: DECLARATIONS, values: {}, options: OPTIONS, ack: 0 } },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'artifact-globals.js' });
  const store = sandbox.__paramStore;
  assert.ok(store && typeof store.setParam === 'function', 'the runtime exposes window.__paramStore');
  // Fires due timers in order; microtasks they queue run before the next one.
  async function advance(ms) {
    const until = now + ms;
    await tick();
    for (;;) {
      let due = null;
      for (const [id, t] of timers) if (t.at <= until && (!due || t.at < due[1].at)) due = [id, t];
      if (!due) break;
      timers.delete(due[0]);
      now = due[1].at;
      due[1].fn();
      await tick();
    }
    now = until;
  }
  // Let the runtime's own start-up timers (chart resizes) run, then count
  // timers from there, and restart the clock at 0.
  return advance(5000).then(() => {
    const baseline = timers.size;
    now = 0;
    for (const t of timers.values()) t.at -= 5000;
    const sets = () => posted.filter((p) => p.msg.type === 'ARTIFACT_SET_PARAMS');
    return { store, advance, sets, pendingTimers: () => timers.size - baseline };
  });
}

const failures = [];
async function check(title, fn) {
  try { await fn(); console.log(`PASS: ${title}`); }
  catch (e) { failures.push(title); console.log(`FAIL: ${title}\n  ${e.message.split('\n')[0]}`); }
}

await check('a param with a declared option list or options_source commits at once', async () => {
  for (const [name, value] of [['region', 'North'], ['owner', 'b']]) {
    const { store, sets, advance } = await boot();
    store.setParam(name, value);
    await advance(0);
    assert.equal(sets().length, 1, `${name}: posted with no wait`);
    assert.deepEqual(sets()[0].msg.changes, { [name]: value });
    assert.equal(sets()[0].at, 0, `${name}: posted at t=0`);
  }
  const { store, sets, advance } = await boot();
  store.setParams({ region: 'South', owner: 'a' });
  await advance(0);
  assert.equal(sets().length, 1, 'setParams of only declared-list params posts at once');
  assert.deepEqual(sets()[0].msg.changes, { region: 'South', owner: 'a' });
});

await check('free text, number, list, id, date range and undeclared params keep the 250 ms debounce', async () => {
  const cases = [
    ['search', ['n', 'no', 'nor']], ['limit', [1, 10, 100]], ['ids', ['a', 'a,b']], ['account', ['7', '77']],
    ['undeclared', ['x', 'xy']],
    ['period', [{ from: '2026-01-01' }, { from: '2026-01-01', to: '2026-01-31' }]],
  ];
  for (const [name, typed] of cases) {
    const { store, sets, advance } = await boot();
    for (const v of typed) { store.setParam(name, v); await advance(100); }
    assert.equal(sets().length, 0, `${name}: nothing posted while typing`);
    await advance(149);
    assert.equal(sets().length, 0, `${name}: still waiting at 249 ms after the last change`);
    await advance(1);
    assert.equal(sets().length, 1, `${name}: one commit 250 ms after the last change`);
    assert.deepEqual(sets()[0].msg.changes, { [name]: typed[typed.length - 1] }, `${name}: the last value wins`);
  }
});

await check('F1: typing a date (one change per intermediate valid date) is one commit', async () => {
  const { store, sets, advance } = await boot();
  for (const v of ['0002-01-31', '0020-01-31', '0202-01-31', '2026-01-31']) { store.setParam('day', v); await advance(60); }
  await advance(250);
  assert.equal(sets().length, 1, `one commit, got ${sets().length}`);
  assert.deepEqual(sets()[0].msg.changes, { day: '2026-01-31' });
});

await check('F2: options the host page derived (params.options only) do not make typing discrete', async () => {
  const { store, sets, advance } = await boot();
  for (const v of ['a', 'ac', 'acm', 'acme']) { store.setParam('customer', v); await advance(60); }
  await advance(250);
  assert.equal(sets().length, 1, `one commit, got ${sets().length}`);
  assert.deepEqual(sets()[0].msg.changes, { customer: 'acme' });
});

await check('F3: several setParam calls in one handler are one commit', async () => {
  const { store, sets, advance } = await boot();
  store.setParam('region', 'East');
  store.setParam('owner', 'a');
  store.setParams({ level: 3 });
  await advance(0);
  assert.equal(sets().length, 1, `one commit, got ${sets().length}`);
  assert.deepEqual(sets()[0].msg.changes, { region: 'East', owner: 'a', level: 3 });
});

await check('F4: setParam then apply(targets) in the same tick keeps the targets', async () => {
  const { store, sets, advance } = await boot();
  store.setParam('region', 'West');
  store.apply(['q1']);
  await advance(500);
  assert.equal(sets().length, 1, `one commit, got ${sets().length}`);
  assert.deepEqual(sets()[0].msg.targets, ['q1']);
  assert.deepEqual(sets()[0].msg.changes, { region: 'West' });
});

await check('F5: a burst of discrete changes is a leading and one trailing commit; repeats are skipped', async () => {
  {
    const { store, sets, advance } = await boot();
    for (let i = 1; i <= 30; i++) { store.setParam('level', i); await advance(5); }
    await advance(1000);
    assert.ok(sets().length <= 2, `at most 2 commits for a 30-change burst, got ${sets().length}`);
    assert.deepEqual(sets()[0].msg.changes, { level: 1 }, 'the first change goes out at once');
    assert.equal(sets()[0].at, 0);
    assert.deepEqual(sets()[sets().length - 1].msg.changes, { level: 30 }, 'the last value wins');
  }
  {
    const { store, sets, advance } = await boot();
    for (let i = 0; i < 3; i++) { store.setParam('region', 'North'); await advance(5); }
    await advance(1000);
    assert.equal(sets().length, 1, `the same value 3 times is one commit, got ${sets().length}`);
  }
});

await check('N1: after a failed run, picking the same value again re-runs it', async () => {
  const { store, sets, advance } = await boot();
  store.setParam('region', 'North');
  await advance(0);
  const seq = sets()[0].msg.seq;
  // The host stores and acks the value, then reports the run failed.
  store._ingest({ params: { declarations: DECLARATIONS, values: { region: 'North' }, options: {}, ack: seq } });
  store._status({ loading: false, error: 'query failed' });
  await advance(1000);
  store.setParam('region', 'North');
  await advance(0);
  assert.equal(sets().length, 2, `the retry is posted, got ${sets().length} commits`);
  assert.deepEqual(sets()[1].msg.changes, { region: 'North' });
});

await check('N5: apply() in the same tick leaves no trailing commit for later held changes', async () => {
  const { store, sets, advance } = await boot();
  store.setParam('region', 'West');
  store.apply(['q1']);
  await advance(100);
  store.setParam('owner', 'a', { apply: false });
  await advance(1000);
  assert.equal(sets().length, 1, `only the applied commit, got ${JSON.stringify(sets().map((p) => p.msg.changes))}`);
  assert.deepEqual({ ...store.getPending() }, { owner: 'a' }, 'owner stays held until Apply');
});

for (const [name, typed] of [['search', ['n']], ['search', ['n', 'no']], ['day', ['0002-01-31']]]) {
  await check(`text edited after the latest pick keeps its own 250 ms debounce through a trailing commit (${name} ${JSON.stringify(typed)})`, async () => {
    const { store, sets, advance } = await boot();
    const label = `${name} ${JSON.stringify(typed)}`;
    store.setParam('region', 'North');          // t=0: leading commit
    await advance(100);
    store.setParam('region', 'South');          // t=100: waits for the trailing commit
    await advance(100);
    for (const v of typed) { store.setParam(name, v); await advance(100); }  // typing from t=200
    const last = 200 + (typed.length - 1) * 100;
    const at = (t) => sets().filter((p) => p.at === t).map((p) => p.msg.changes);
    await advance(1000);
    assert.deepEqual(at(0), [{ region: 'North' }], `${label}: the first pick at once`);
    assert.deepEqual(at(250), [{ region: 'South' }], `${label}: the trailing commit carries the pick only`);
    const withText = sets().filter((p) => name in p.msg.changes);
    assert.equal(withText.length, 1, `${label}: the text goes out once, got ${JSON.stringify(sets().map((p) => [p.at, p.msg.changes]))}`);
    assert.equal(withText[0].at, last + 250, `${label}: 250 ms after the last edit`);
    assert.deepEqual(withText[0].msg.changes, { [name]: typed[typed.length - 1] }, `${label}: the last value`);
    const seqs = sets().map((p) => p.msg.seq);
    assert.ok(seqs.every((s, i) => i === 0 || s > seqs[i - 1]), `${label}: seq increases: ${seqs}`);
  });
}

await check('text pending before the latest pick still rides with the trailing commit', async () => {
  const { store, sets, advance } = await boot();
  store.setParam('region', 'North');
  await advance(50);
  store.setParam('search', 'ab');               // t=50, before the next pick
  await advance(50);
  store.setParam('region', 'South');            // t=100
  await advance(1000);
  assert.equal(sets().length, 2, `two commits, got ${JSON.stringify(sets().map((p) => [p.at, p.msg.changes]))}`);
  assert.equal(sets()[1].at, 250);
  assert.deepEqual(sets()[1].msg.changes, { search: 'ab', region: 'South' });
});

await check('mixed setParams (declared list + free text) waits 250 ms', async () => {
  const { store, sets, advance } = await boot();
  store.setParams({ region: 'North', search: 'abc' });
  await advance(249);
  assert.equal(sets().length, 0, 'a mixed setParams does not commit at once');
  await advance(1);
  assert.equal(sets().length, 1);
  assert.deepEqual(sets()[0].msg.changes, { region: 'North', search: 'abc' });
});

await check('apply:false holds every change; apply() sends them with its targets', async () => {
  const { store, sets, advance, pendingTimers } = await boot();
  store.setParam('region', 'North', { apply: false });
  store.setParams({ day: '2026-03-01' }, { apply: false });
  store.setParam('search', 'q', { apply: false });
  await advance(1000);
  assert.equal(sets().length, 0, 'apply:false holds every change');
  assert.equal(pendingTimers(), 0);
  assert.deepEqual({ ...store.getPending() }, { region: 'North', day: '2026-03-01', search: 'q' });
  store.apply(['viz-1']);
  assert.equal(sets().length, 1, 'apply() commits what was held');
  assert.deepEqual(sets()[0].msg.targets, ['viz-1']);
});

await check('a declared-list change while typed text is pending sends both now, as one commit', async () => {
  const { store, sets, advance } = await boot();
  store.setParam('search', 'ab');
  await advance(100);
  store.setParam('region', 'South');
  await advance(0);
  assert.equal(sets().length, 1, 'the discrete change flushes the pending text');
  assert.deepEqual(sets()[0].msg.changes, { search: 'ab', region: 'South' });
  await advance(1000);
  assert.equal(sets().length, 1, 'and no second commit follows');
});

await check('commit sequence numbers increase and stale replies are handled', async () => {
  const { store, sets, advance } = await boot();
  store.setParam('region', 'North');
  store.setParam('search', 'x');
  await advance(250);
  store.setParam('owner', 'b');
  await advance(250);
  const seqs = sets().map((p) => p.msg.seq);
  assert.equal(seqs.length, 2, `two commits: ${JSON.stringify(sets().map((p) => p.msg.changes))}`);
  // search was typed in the same tick as region, so it rode the first commit.
  assert.deepEqual(sets()[0].msg.changes, { region: 'North', search: 'x' });
  assert.ok(seqs[0] < seqs[1], `seq increases per commit: ${seqs}`);
  // The host acks only the first commit: the later one stays in flight.
  store._ingest({ params: { declarations: DECLARATIONS, values: { region: 'North', search: 'x' }, options: {}, ack: seqs[0] } });
  assert.equal(store.getValues().owner, 'b', 'a stale reply keeps the newer in-flight value');
  assert.equal(store.isLoading(), true, 'still loading until the newer commit is acked');
  store._ingest({ params: { declarations: DECLARATIONS, values: { region: 'North', search: 'x', owner: 'b' }, options: {}, ack: seqs[1] } });
  assert.equal(store.isLoading(), false, 'the newest ack settles everything');
});

if (failures.length) {
  console.log(`\n${failures.length} failing: ${failures.join(' | ')}`);
  process.exit(1);
}
