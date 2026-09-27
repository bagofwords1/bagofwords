<script type="text/babel">
// Demo app for artifact app persistence (tools/agent/seed_app_persistence_demo.py).
// Reuses the source dashboard's two visualizations and adds three collections:
//   notes      shared, members add, authors edit their own (the owner edits any)
//   selections per_user, remembers the viewer's genre choice
//   highlights shared, owner only (the headline strip)
// Every write is caught; failures render through each collection's `error`.
// No form submit anywhere: artifact iframes are sandboxed without allow-forms.
setTheme('atelier', { radius: 'crisp', shadow: 'soft' });

// Substituted by the seed script with the source artifact's visualization ids.
const REVENUE_VIZ_ID = '__REVENUE_VIZ_ID__';
const CUSTOMERS_VIZ_ID = '__CUSTOMERS_VIZ_ID__';

const ERROR_TEXT = {
  forbidden: 'Not allowed: only the author or the report owner can change this.',
  unauthenticated: 'Sign in to use this.',
  conflict: 'Someone changed this a moment ago. The latest version is shown; try again.',
  validation: 'That value cannot be saved. Check the text and try again.',
  not_found: 'That item no longer exists. The list was refreshed.',
  too_large: 'That is too long to save.',
  limit_reached: 'This collection is full.',
  collection_not_declared: 'This app is not set up to store that.',
  unavailable: 'Saving is not available in this view.',
  timeout: 'The server did not answer in time. Try again.',
  network: 'Network problem. Try again.',
};

function errorText(err, overrides) {
  if (!err) return '';
  return (overrides && overrides[err.code]) || ERROR_TEXT[err.code] || err.message || 'Something went wrong.';
}

function ErrorLine({ error, testid, overrides }) {
  if (!error) return null;
  return (
    <p role="alert" data-testid={testid} data-code={error.code}
       className="flex items-start gap-1.5 text-[13px] text-negative bg-negative/10 rounded-control px-2.5 py-1.5">
      <Icon name="alert-triangle" size={14} className="mt-0.5 shrink-0" />
      <span>{errorText(error, overrides)}</span>
    </p>
  );
}

function whenText(iso) {
  if (!iso) return '';
  const t = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z').getTime();
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

function pickViz(visualizations, id, pattern, fallbackIndex) {
  const byId = vizById(id);
  if (byId) return byId;
  const list = visualizations || [];
  return list.find(v => pattern.test(String(v.title || ''))) || list[fallbackIndex] || null;
}

function isNumeric(v) {
  return v !== null && v !== '' && !isNaN(Number(v));
}

// Aggregate a visualization into [{ country, value }], largest first.
function byCountry(viz, valuePattern) {
  if (!viz || !Array.isArray(viz.rows) || !viz.rows.length) return [];
  const cols = (viz.columns || []).map(c => String(c.field));
  const fields = cols.length ? cols : Object.keys(viz.rows[0] || {});
  const sample = viz.rows[0] || {};
  const countryField = fields.find(f => /country/i.test(f)) || fields.find(f => !isNumeric(sample[f])) || fields[0];
  const numeric = fields.filter(f => f !== countryField && isNumeric(sample[f]));
  const valueField = numeric.find(f => valuePattern.test(f)) || numeric[0];
  if (!countryField || !valueField) return [];
  const totals = new Map();
  viz.rows.forEach(r => {
    const key = String(r[countryField] ?? 'Unknown');
    totals.set(key, (totals.get(key) || 0) + Number(r[valueField] || 0));
  });
  return Array.from(totals, ([country, value]) => ({ country, value })).sort((a, b) => b.value - a.value);
}

function barOption(rows, color, currency) {
  const top = rows.slice(0, 12).reverse();
  return {
    grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: 'value', axisLabel: { formatter: v => fmt(v, currency ? { currency: true } : {}) } },
    yAxis: { type: 'category', data: top.map(r => r.country) },
    tooltip: { trigger: 'axis', valueFormatter: v => fmt(v, currency ? { currency: true } : {}) },
    series: [{ type: 'bar', data: top.map(r => r.value), itemStyle: { color }, barMaxWidth: 18 }],
  };
}

function HighlightStrip({ signedIn }) {
  const highlights = useCollection("highlights");
  const [draft, setDraft] = useState('');
  const publish = () => {
    const text = draft.trim();
    if (!text) return;
    highlights.add({ text }).then(() => setDraft('')).catch(() => {});
  };
  const drop = (id) => { highlights.remove(id).catch(() => {}); };
  return (
    <section data-testid="highlights" className="rounded-card bg-surface-2 border border-line px-4 py-3">
      <div className="flex items-center gap-2 mb-2">
        <Icon name="sparkles" size={15} className="text-accent" />
        <Eyebrow className="text-[11px] font-medium uppercase tracking-eyebrow text-accent">Highlights from the owner</Eyebrow>
        {highlights.loading && <LoadingSpinner size={14} />}
      </div>
      <ErrorLine error={highlights.error} testid="highlights-error"
                 overrides={{ forbidden: 'Only the report owner can publish or remove highlights.' }} />
      <div className="flex gap-2 overflow-x-auto pb-1">
        {highlights.items.map(item => (
          <div key={item.id} data-testid="highlight-item" data-id={item.id}
               className="group shrink-0 max-w-xs flex items-start gap-2 rounded-control bg-surface border border-line px-3 py-2 shadow-card">
            <span className="text-[13px] text-ink leading-snug">{item.data.text}</span>
            {signedIn && (
              <button type="button" aria-label="Remove highlight" data-testid="highlight-delete" onClick={() => drop(item.id)}
                      className="text-ink-3 hover:text-negative opacity-60 group-hover:opacity-100">
                <Icon name="x" size={13} />
              </button>
            )}
          </div>
        ))}
        {!highlights.loading && !highlights.items.length && !highlights.error && (
          <span className="text-[13px] text-ink-3">No highlights yet.</span>
        )}
      </div>
      {signedIn && (
        <div className="mt-2 flex gap-2">
          <input data-testid="highlight-input" value={draft} maxLength={280} onChange={e => setDraft(e.target.value)}
                 onKeyDown={e => { if (e.key === 'Enter') publish(); }}
                 placeholder="Publish a headline (owner only)"
                 className="flex-1 min-w-0 rounded-control border border-line bg-surface px-2.5 py-1.5 text-[13px] text-ink" />
          <button type="button" data-testid="highlight-add" disabled={!draft.trim() || highlights.loading} onClick={publish}
                  className="rounded-control bg-accent text-accent-ink px-3 py-1.5 text-[13px] font-medium disabled:opacity-50">Publish</button>
        </div>
      )}
    </section>
  );
}

function NotesPanel({ signedIn, countries, focus, setFocus }) {
  const notes = useCollection("notes");
  const [text, setText] = useState('');
  const [country, setCountry] = useState('');
  const [editing, setEditing] = useState(null);
  const [editText, setEditText] = useState('');
  const target = country || focus || (countries[0] || '');
  const shown = focus ? notes.items.filter(n => n.data.country === focus) : notes.items;
  const counts = useMemo(() => {
    const m = new Map();
    notes.items.forEach(n => m.set(n.data.country, (m.get(n.data.country) || 0) + 1));
    return m;
  }, [notes.items]);

  const add = () => {
    const body = text.trim();
    if (!body || !target) return;
    notes.add({ country: target, text: body }).then(() => setText('')).catch(() => {});
  };
  const startEdit = (n) => { setEditing(n.id); setEditText(n.data.text); };
  const saveEdit = (id) => {
    notes.update(id, { text: editText.trim() })
      .then(() => setEditing(null))
      .catch(e => { if (e.code !== 'validation' && e.code !== 'too_large') setEditing(null); });
  };
  const drop = (id) => { notes.remove(id).catch(() => {}); };

  return (
    <SectionCard title="Notes by country" subtitle="Shared with everyone who can open this report"
                 className="h-full" bodyClassName="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <select data-testid="notes-filter" value={focus} onChange={e => setFocus(e.target.value)}
                className="flex-1 min-w-0 rounded-control border border-line bg-surface px-2 py-1.5 text-[13px] text-ink">
          <option value="">All countries ({notes.items.length})</option>
          {countries.map(c => <option key={c} value={c}>{c}{counts.get(c) ? ` (${counts.get(c)})` : ''}</option>)}
        </select>
        {notes.loading && <LoadingSpinner size={16} />}
      </div>
      <ErrorLine error={notes.error} testid="notes-error"
                 overrides={{ unauthenticated: 'Sign in to read and write notes.' }} />
      {notes.loading && !notes.items.length && !notes.error ? (
        <div className="py-6 flex justify-center"><LoadingSpinner size={22} /></div>
      ) : (
        <ul data-testid="notes-list" className="flex flex-col gap-2 max-h-[420px] overflow-y-auto">
          {shown.map(n => (
            <li key={n.id} data-testid="note-item" data-id={n.id} data-mine={String(!!n.mine)}
                data-author={n.user?.name ?? ''} data-version={n.version}
                className={'rounded-control border px-3 py-2 ' + (n.mine ? 'border-accent/40 bg-accent/5' : 'border-line bg-surface')}>
              <div className="flex items-center gap-1.5 flex-wrap text-[11px] text-ink-3">
                <Badge tone="outline">{n.data.country}</Badge>
                <span data-testid="note-author" className="font-medium text-ink-2">{n.user?.name ?? 'Someone'}</span>
                {n.mine && <span data-testid="note-mine"><Badge tone="accent">mine</Badge></span>}
                <span className="ms-auto">{whenText(n.updated_at)}{n.version > 1 ? ' · edited' : ''}</span>
              </div>
              {editing === n.id ? (
                <div className="mt-1.5 flex flex-col gap-1.5">
                  <textarea data-testid="note-edit-input" value={editText} rows={2} maxLength={2000}
                            onChange={e => setEditText(e.target.value)}
                            className="rounded-control border border-line bg-surface px-2 py-1 text-[13px] text-ink" />
                  <div className="flex gap-2 justify-end">
                    <button type="button" onClick={() => setEditing(null)} className="text-[12px] text-ink-3">Cancel</button>
                    <button type="button" data-testid="note-save" disabled={!editText.trim()} onClick={() => saveEdit(n.id)}
                            className="rounded-control bg-accent text-accent-ink px-2.5 py-1 text-[12px] font-medium disabled:opacity-50">Save</button>
                  </div>
                </div>
              ) : (
                <p data-testid="note-text" className="mt-1 text-[13px] text-ink leading-snug whitespace-pre-wrap">{n.data.text}</p>
              )}
              {signedIn && editing !== n.id && (
                <div className="mt-1.5 flex gap-3 text-[12px]">
                  <button type="button" data-testid="note-edit" onClick={() => startEdit(n)} className="text-ink-3 hover:text-accent">Edit</button>
                  <button type="button" data-testid="note-delete" onClick={() => drop(n.id)} className="text-ink-3 hover:text-negative">Delete</button>
                </div>
              )}
            </li>
          ))}
          {!shown.length && !notes.error && (
            <EmptyState icon="message-square">{focus ? `No notes for ${focus} yet` : 'No notes yet'}</EmptyState>
          )}
        </ul>
      )}
      {signedIn ? (
        <div className="flex flex-col gap-2 border-t border-line pt-3">
          <div className="flex gap-2">
            {countries.length ? (
              <select data-testid="note-country" value={target} onChange={e => setCountry(e.target.value)}
                      className="w-40 rounded-control border border-line bg-surface px-2 py-1.5 text-[13px] text-ink">
                {countries.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            ) : (
              <input data-testid="note-country" value={country} onChange={e => setCountry(e.target.value)} placeholder="Country"
                     className="w-40 rounded-control border border-line bg-surface px-2 py-1.5 text-[13px] text-ink" />
            )}
            <input data-testid="note-input" value={text} maxLength={2000} onChange={e => setText(e.target.value)}
                   onKeyDown={e => { if (e.key === 'Enter') add(); }}
                   placeholder="Add a note…"
                   className="flex-1 min-w-0 rounded-control border border-line bg-surface px-2.5 py-1.5 text-[13px] text-ink" />
          </div>
          <button type="button" data-testid="note-add" disabled={!text.trim() || !target || notes.loading} onClick={add}
                  className="self-end rounded-control bg-accent text-accent-ink px-3 py-1.5 text-[13px] font-medium disabled:opacity-50">Add note</button>
        </div>
      ) : (
        <p className="text-[12px] text-ink-3 border-t border-line pt-3">Sign in to add notes.</p>
      )}
    </SectionCard>
  );
}

function GenreControl({ signedIn }) {
  const params = useParams();
  const selections = useCollection("selections");
  const decl = (params.declarations || []).find(d => /genre/i.test(String(d.name)) && d.source !== 'identity') || null;
  const name = decl ? decl.name : null;
  const options = useParamOptions(name || '__no_genre_param__') || [];
  const mine = selections.items.length ? selections.items[selections.items.length - 1] : null;
  const saved = mine ? mine.data.genre : undefined;
  const restored = useRef(false);

  // Restore the viewer's saved genre once, after their selection has loaded.
  useEffect(() => {
    if (restored.current || !name || selections.loading) return;
    restored.current = true;
    if (saved !== undefined && JSON.stringify(saved) !== JSON.stringify(params.values[name] ?? null)) {
      params.setParam(name, saved);
    }
  }, [name, selections.loading, saved]);

  const choose = (raw) => {
    const value = raw === '' ? null : raw;
    if (name) params.setParam(name, value);
    if (!signedIn) return;
    const write = mine ? selections.update(mine.id, { genre: value }) : selections.add({ genre: value });
    write.catch(() => {});
  };

  if (!name) return null;
  const current = params.values[name] ?? '';
  const busy = selections.loading && !mine && !selections.error;
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <label className="text-[12px] text-ink-3" htmlFor="genre-select">{decl.label || 'Genre'}</label>
      <select id="genre-select" data-testid="genre-select" value={current === null ? '' : String(current)} disabled={busy}
              onChange={e => choose(e.target.value)}
              className="rounded-control border border-line bg-surface px-2 py-1.5 text-[13px] text-ink min-w-[10rem]">
        <option value="">All genres</option>
        {options.map(o => <option key={String(o.value)} value={String(o.value)}>{o.label ?? String(o.value)}</option>)}
      </select>
      {signedIn && mine && (
        <span data-testid="saved-genre" data-genre={saved ?? ''}>
          <Badge tone="accent" icon="bookmark">Remembered for you: {saved ?? 'All genres'}</Badge>
        </span>
      )}
      {!signedIn && <span className="text-[12px] text-ink-3">Sign in to remember your choice</span>}
      {params.loading && <LoadingSpinner size={14} />}
      {signedIn && <ErrorLine error={selections.error} testid="selections-error" />}
      {params.error && (
        <p data-testid="params-error" className="text-[12px] text-negative">{String(params.error.message || params.error)}</p>
      )}
    </div>
  );
}

function App() {
  const data = useArtifactData();
  const user = useCurrentUser();
  const theme = useTheme();
  const [focus, setFocus] = useState('');
  const visualizations = (data && data.visualizations) || [];
  const revenueViz = pickViz(visualizations, REVENUE_VIZ_ID, /revenue|sales/i, 0);
  const customersViz = pickViz(visualizations, CUSTOMERS_VIZ_ID, /customer/i, 1);
  const revenue = useMemo(() => byCountry(revenueViz, /revenue|total|sales|amount/i), [revenueViz]);
  const customers = useMemo(() => byCountry(customersViz, /customer|count|num/i), [customersViz]);
  const countries = useMemo(() => {
    const all = new Set([...revenue.map(r => r.country), ...customers.map(r => r.country)]);
    return Array.from(all).sort((a, b) => a.localeCompare(b));
  }, [revenue, customers]);
  const signedIn = !!(user && user.id);
  const colors = theme.colors || {};

  if (!data) {
    return <div className="min-h-[60vh] flex items-center justify-center"><LoadingSpinner size={28} /></div>;
  }
  return (
    <main data-testid="app-root" className="min-h-screen bg-bg text-ink font-body px-5 py-6 md:px-8 flex flex-col gap-5">
      <PageHeader eyebrow="Country revenue" title="Revenue by Country — with notes"
                  subtitle="Notes are shared with everyone on this report. Your genre choice is remembered just for you."
                  actions={[<GenreControl key="g" signedIn={signedIn} />]} />
      <HighlightStrip signedIn={signedIn} />
      <div className="grid gap-5 lg:grid-cols-3">
        <div className="lg:col-span-2 flex flex-col gap-5">
          <SectionCard title="Revenue by country" subtitle="Top 12 · click a bar to see its notes" viz={revenueViz || undefined}>
            <div data-testid="revenue-chart">
              {revenue.length
                ? <EChart height={320} option={barOption(revenue, colors.accent, true)} onClick={p => setFocus(p.name)} />
                : <EmptyState icon="bar-chart-3">No revenue rows for this selection</EmptyState>}
            </div>
          </SectionCard>
          <SectionCard title="Customers per country" subtitle="Top 12" viz={customersViz || undefined}>
            <div data-testid="customers-chart">
              {customers.length
                ? <EChart height={280} option={barOption(customers, colors.accent2, false)} onClick={p => setFocus(p.name)} />
                : <EmptyState icon="users">No customer rows</EmptyState>}
            </div>
          </SectionCard>
        </div>
        <NotesPanel signedIn={signedIn} countries={countries} focus={focus} setFocus={setFocus} />
      </div>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
