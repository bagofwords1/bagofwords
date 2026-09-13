<script type="text/babel">
setTheme('slate', {
  accent: '#177c78',
  accent2: '#d79555',
  chart: ['#177c78', '#d79555', '#28475c', '#8aa6a4', '#b86f52', '#6c7890'],
  radius: 'crisp',
  shadow: 'soft'
});

const WEEKLY_ID = 'FIXTURE_VIZ_0';
const MARKET_ID = 'FIXTURE_VIZ_1';
const CUSTOMER_ID = 'FIXTURE_VIZ_2';

function Field({ label, children, hint }) {
  return (
    <label className="cw-field">
      <span className="cw-label">{label}</span>
      {children}
      {hint && <span className="cw-hint">{hint}</span>}
    </label>
  );
}

function App() {
  const data = useArtifactData();
  const theme = useTheme();
  const params = useParams();
  const regionOptions = useParamOptions('region') || [];
  const channelOptions = useParamOptions('channels') || [];

  const weeklyViz = vizById(WEEKLY_ID);
  const marketViz = vizById(MARKET_ID);
  const customerViz = vizById(CUSTOMER_ID);

  const [draft, setDraft] = useState({
    region: null,
    channels: ['Direct', 'Partner', 'Marketplace'],
    period: { from: '2026-01-01', to: '2026-06-30' },
    min_order: 0
  });
  const [customerSearch, setCustomerSearch] = useState('');
  const [selectedId, setSelectedId] = useState(null);
  const [view, setView] = useState('chart');

  useEffect(() => {
    if (params.values) {
      setDraft({
        region: params.values.region ?? null,
        channels: params.values.channels ?? ['Direct', 'Partner', 'Marketplace'],
        period: params.values.period ?? { from: '2026-01-01', to: '2026-06-30' },
        min_order: params.values.min_order ?? 0
      });
    }
  }, [params.values?.region, params.values?.channels, params.values?.period, params.values?.min_order]);

  const weeklyRows = weeklyViz?.rows || [];
  const marketRows = marketViz?.rows || [];
  const customerRows = customerViz?.rows || [];

  const visibleCustomers = useMemo(() => {
    const term = customerSearch.trim().toLowerCase();
    if (!term) return customerRows;
    return customerRows.filter(row =>
      [row.customer, row.customer_id, row.region, row.segment, row.representative]
        .some(value => String(value ?? '').toLowerCase().includes(term))
    );
  }, [customerRows, customerSearch]);

  useEffect(() => {
    if (!visibleCustomers.some(row => String(row.customer_id) === String(selectedId))) {
      setSelectedId(visibleCustomers[0]?.customer_id ?? null);
    }
  }, [visibleCustomers, selectedId]);

  const selectedCustomer = customerRows.find(
    row => String(row.customer_id) === String(selectedId)
  );

  const revenueTotal = weeklyRows.reduce((sum, row) => sum + (Number(row.revenue) || 0), 0);
  const marginTotal = weeklyRows.reduce((sum, row) => sum + (Number(row.margin) || 0), 0);
  const orderTotal = weeklyRows.reduce((sum, row) => sum + (Number(row.orders) || 0), 0);
  const marginRate = revenueTotal ? marginTotal / revenueTotal : null;

  const marketTotals = useMemo(() => {
    const grouped = new Map();
    marketRows.forEach(row => {
      const region = String(row.region ?? 'Unknown');
      const prior = grouped.get(region) || { region, revenue: 0, orders: 0 };
      prior.revenue += Number(row.revenue) || 0;
      prior.orders += Number(row.orders) || 0;
      grouped.set(region, prior);
    });
    return Array.from(grouped.values()).sort((a, b) => b.revenue - a.revenue);
  }, [marketRows]);

  const toggleChannel = value => {
    const current = draft.channels || [];
    const next = current.includes(value)
      ? current.filter(channel => channel !== value)
      : [...current, value];
    setDraft({ ...draft, channels: next });
  };

  const applyParameters = () => {
    params.setParams({
      region: draft.region || null,
      channels: draft.channels,
      period: draft.period,
      min_order: Number(draft.min_order) || 0
    }, { apply: false });
    params.apply();
  };

  if (!data) {
    return (
      <div className="cw-shell">
        <LoadingSpinner size={28} />
        <span className="cw-loading">Loading commerce workspace…</span>
      </div>
    );
  }

  const weeklyOption = {
    tooltip: { trigger: 'axis', valueFormatter: value => fmt(value, { currency: true }) },
    legend: { data: ['Revenue', 'Margin'], bottom: 0 },
    xAxis: { type: 'category', data: weeklyRows.map(row => row.week) },
    yAxis: { type: 'value', axisLabel: { formatter: value => fmt(value, { compact: true, currency: true }) } },
    series: [
      {
        name: 'Revenue',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        data: weeklyRows.map(row => row.revenue),
        lineStyle: { width: 3, color: theme.colors.accent },
        itemStyle: { color: theme.colors.accent },
        areaStyle: { color: 'rgba(23,124,120,.10)' }
      },
      {
        name: 'Margin',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        data: weeklyRows.map(row => row.margin),
        lineStyle: { width: 2, color: theme.colors.accent2 },
        itemStyle: { color: theme.colors.accent2 }
      }
    ]
  };

  const marketOption = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: value => fmt(value, { currency: true })
    },
    grid: { left: 4, right: 12, top: 10, bottom: 10, containLabel: true },
    xAxis: { type: 'value', splitNumber: 3, axisLabel: { hideOverlap: true, formatter: value => '$' + fmt(value) } },
    yAxis: { type: 'category', data: marketTotals.map(row => row.region) },
    series: [{
      type: 'bar',
      data: marketTotals.map(row => row.revenue),
      barMaxWidth: 22,
      itemStyle: { color: theme.colors.accent, borderRadius: [0, 4, 4, 0] }
    }]
  };

  return (
    <div className="cw-shell">
      <style>{`
        .cw-shell{min-height:100vh;background:#f8f7f3;color:#20313c;font-family:Inter,ui-sans-serif,system-ui,sans-serif;padding:28px 30px 44px}
        .cw-wrap{max-width:1120px;margin:0 auto}
        .cw-top{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:26px}
        .cw-kicker{font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:#177c78;font-weight:700;margin-bottom:8px}
        .cw-title{font-size:31px;line-height:1.1;letter-spacing:-.04em;font-weight:650;color:#20313c;margin:0}
        .cw-subtitle{font-size:14px;color:#6b7880;margin:9px 0 0;max-width:680px}
        .cw-status{display:flex;align-items:center;gap:8px;color:#65737b;font-size:12px;padding-top:5px;white-space:nowrap}
        .cw-dot{width:7px;height:7px;border-radius:50%;background:#177c78}
        .cw-controls{background:#fff;border:1px solid #e2e3de;border-radius:10px;padding:16px 18px;margin-bottom:20px;box-shadow:0 5px 18px rgba(31,47,57,.045)}
        .cw-control-grid{display:grid;grid-template-columns:minmax(272px,1.3fr) 1.3fr 1fr .9fr auto;gap:13px;align-items:end}
        .cw-field{display:flex;flex-direction:column;gap:7px;min-width:0}
        .cw-label{font-size:11px;font-weight:700;color:#66747a;letter-spacing:.04em}
        .cw-input,.cw-select{height:36px;border:1px solid #d9dedb;border-radius:6px;background:#fbfbf9;color:#263942;padding:0 10px;font-size:13px;outline:none;min-width:0}
        .cw-input:focus,.cw-select:focus{border-color:#177c78;box-shadow:0 0 0 3px rgba(23,124,120,.12)}
        .cw-channels{display:flex;gap:6px;flex-wrap:wrap}
        .cw-chip{border:1px solid #d8dfdc;background:#fff;color:#617077;border-radius:999px;padding:8px 10px;font-size:12px;cursor:pointer}
        .cw-chip.active{background:#e5f2f0;border-color:#8bc3bd;color:#126b68;font-weight:700}
        .cw-button{height:36px;border:0;border-radius:6px;padding:0 17px;background:#177c78;color:white;font-weight:700;font-size:12px;cursor:pointer;white-space:nowrap}
        .cw-button:hover{background:#126b68}.cw-button:disabled{opacity:.55;cursor:wait}
        .cw-hint{font-size:11px;color:#899398}
        .cw-error{font-size:12px;color:#b65345;background:#fff1ed;border:1px solid #f0cfc7;padding:9px 11px;border-radius:6px;margin-top:12px}
        .cw-applied{font-size:11px;color:#7b878b;align-self:center;padding-bottom:9px}
        .cw-grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(290px,.85fr);gap:18px}
        .cw-panel{background:#fff;border:1px solid #e2e3de;border-radius:10px;box-shadow:0 5px 18px rgba(31,47,57,.04);overflow:hidden}
        .cw-panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:15px;padding:18px 19px 10px}
        .cw-panel-title{font-size:15px;font-weight:700;color:#243943;margin:0}
        .cw-panel-sub{font-size:12px;color:#879196;margin:5px 0 0}
        .cw-chart{padding:2px 12px 10px}
        .cw-strip{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid #edf0ed;margin:4px 18px 0;padding:14px 0 16px;gap:14px}
        .cw-stat-label{font-size:11px;color:#879196}.cw-stat-value{font-size:18px;font-weight:700;color:#263c46;margin-top:4px;font-variant-numeric:tabular-nums}
        .cw-market{padding:0 12px 16px}
        .cw-table-panel{margin-top:18px}
        .cw-table-head{display:flex;justify-content:space-between;align-items:center;padding:17px 19px 13px;gap:12px}
        .cw-search{height:34px;border:1px solid #d9dedb;border-radius:6px;padding:0 10px;min-width:180px;font-size:12px;background:#fbfbf9}
        .cw-tabs{display:flex;gap:3px;border-bottom:1px solid #e8ebe8;padding:0 19px}
        .cw-tab{border:0;background:transparent;padding:10px 12px;color:#7c888d;font-size:12px;cursor:pointer;border-bottom:2px solid transparent}
        .cw-tab.active{color:#177c78;border-bottom-color:#177c78;font-weight:700}
        .cw-detail{padding:0 19px 19px}
        .cw-customer-name{font-size:20px;font-weight:700;color:#243943;margin:4px 0 6px}
        .cw-detail-meta{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:17px}
        .cw-badge{font-size:11px;border-radius:999px;background:#eef3f1;color:#58706f;padding:5px 9px}
        .cw-detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
        .cw-detail-cell{border-top:1px solid #e9ece9;padding-top:10px}.cw-detail-cell span{display:block;font-size:11px;color:#899398}.cw-detail-cell strong{display:block;margin-top:4px;font-size:14px;color:#334a54}
        .cw-empty{padding:28px 18px;color:#7c888d;font-size:13px}
        .cw-loading{margin-left:10px;color:#718087;font-size:13px}
        @media(max-width:1100px){.cw-control-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.cw-control-grid .cw-apply{grid-column:span 2}}
        @media(max-width:780px){.cw-shell{padding:20px 15px 35px}.cw-top{display:block}.cw-status{margin-top:14px}.cw-control-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.cw-control-grid .cw-apply{grid-column:span 2}.cw-grid{grid-template-columns:1fr}.cw-table-head{align-items:flex-start;flex-direction:column}.cw-search{width:100%;min-width:0}}
        @media(max-width:460px){.cw-control-grid{grid-template-columns:1fr}.cw-control-grid .cw-apply{grid-column:auto}.cw-strip{grid-template-columns:1fr 1fr}.cw-strip .cw-stat:last-child{grid-column:span 2}.cw-title{font-size:27px}}
      `}</style>

      <div className="cw-wrap">
        <header className="cw-top">
          <div>
            <div className="cw-kicker">Commerce / Workbench</div>
            <h1 className="cw-title">Commerce Workbench</h1>
            <p className="cw-subtitle">Weekly revenue and margin analysis with market context and customer-level follow-through.</p>
          </div>
          <div className="cw-status"><span className="cw-dot"></span>{params.loading ? 'Updating data…' : 'Connected to source data'}</div>
        </header>

        <section className="cw-controls" aria-label="Analysis controls">
          <div className="cw-control-grid">
            <Field label="Period">
              <div style={{display:'flex',gap:6}}>
                <input className="cw-input" type="date" aria-label="Period from" value={draft.period?.from || ''} onChange={e => setDraft({...draft, period:{...draft.period, from:e.target.value}})} />
                <input className="cw-input" type="date" aria-label="Period to" value={draft.period?.to || ''} onChange={e => setDraft({...draft, period:{...draft.period, to:e.target.value}})} />
              </div>
            </Field>
            <Field label="Channels">
              <div className="cw-channels">
                {channelOptions.map(option => (
                  <button type="button" key={option.value} className={`cw-chip ${(draft.channels || []).includes(option.value) ? 'active' : ''}`} onClick={() => toggleChannel(option.value)}>
                    {option.label}
                  </button>
                ))}
              </div>
            </Field>
            <Field label="Region">
              <select className="cw-select" value={draft.region || ''} onChange={e => setDraft({...draft, region:e.target.value || null})}>
                <option value="">All regions</option>
                {regionOptions.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </Field>
            <Field label="Minimum order value" hint="Applied server-side">
              <input className="cw-input" type="number" min="0" step="1" value={draft.min_order ?? 0} onChange={e => setDraft({...draft, min_order:e.target.value === '' ? 0 : Number(e.target.value)})} />
            </Field>
            <div className="cw-apply">
              <button type="button" className="cw-button" onClick={applyParameters} disabled={params.loading}>Apply filters</button>
            </div>
          </div>
          {params.error && <div className="cw-error" role="alert">{String(params.error)}</div>}
        </section>

        <main className="cw-grid">
          <section className="cw-panel" data-bow-viz={WEEKLY_ID} data-bow-calc="SUM(revenue), SUM(margin), SUM(orders)">
            <div className="cw-panel-head">
              <div>
                <h2 className="cw-panel-title">Weekly performance</h2>
                <p className="cw-panel-sub">Revenue and contribution margin across the selected period</p>
              </div>
              <Badge tone="accent">{weeklyRows.length} weeks</Badge>
            </div>
            {weeklyRows.length ? <div className="cw-chart"><EChart height={285} option={weeklyOption} viz={weeklyViz} rows={weeklyRows} calc="SUM(revenue), SUM(margin)" /></div> : <div className="cw-empty">No weekly results for this selection.</div>}
            <div className="cw-strip">
              <div className="cw-stat"><div className="cw-stat-label">Revenue</div><div className="cw-stat-value">{fmt(revenueTotal,{currency:true,compact:true})}</div></div>
              <div className="cw-stat"><div className="cw-stat-label">Margin</div><div className="cw-stat-value">{fmt(marginTotal,{currency:true,compact:true})}</div></div>
              <div className="cw-stat"><div className="cw-stat-label">Margin rate</div><div className="cw-stat-value">{marginRate == null ? '—' : fmt(marginRate,{pct:true,ratio:true})}</div></div>
            </div>
          </section>

          <section className="cw-panel" data-bow-viz={MARKET_ID} data-bow-calc="SUM(revenue) GROUP BY region">
            <div className="cw-panel-head">
              <div>
                <h2 className="cw-panel-title">Market comparison</h2>
                <p className="cw-panel-sub">Aggregated revenue by region</p>
              </div>
              <span className="cw-applied">{fmt(orderTotal,{compact:true})} orders</span>
            </div>
            {marketTotals.length ? <div className="cw-market"><EChart height={285} option={marketOption} viz={marketViz} rows={marketRows} calc="SUM(revenue) GROUP BY region" /></div> : <div className="cw-empty">No market results for this selection.</div>}
          </section>
        </main>

        <section className="cw-panel cw-table-panel" data-bow-viz={CUSTOMER_ID} data-bow-calc="Customer portfolio rows">
          <div className="cw-table-head">
            <div>
              <h2 className="cw-panel-title">Customer portfolio</h2>
              <p className="cw-panel-sub">Search the fetched customer rows, then inspect a customer profile.</p>
            </div>
            <input className="cw-search" aria-label="Search customers" placeholder="Search customers…" value={customerSearch} onChange={e => setCustomerSearch(e.target.value)} />
          </div>
          <div className="cw-tabs">
            <button className={`cw-tab ${view === 'chart' ? 'active' : ''}`} onClick={() => setView('chart')}>Customer detail</button>
            <button className={`cw-tab ${view === 'table' ? 'active' : ''}`} onClick={() => setView('table')}>Portfolio table</button>
          </div>

          {view === 'chart' ? (
            selectedCustomer ? (
              <div className="cw-detail">
                <div className="cw-customer-name">{selectedCustomer.customer}</div>
                <div className="cw-detail-meta">
                  <span className="cw-badge">{selectedCustomer.segment}</span>
                  <span className="cw-badge">{selectedCustomer.region}</span>
                  <span className="cw-badge">Rep: {selectedCustomer.representative}</span>
                </div>
                <div className="cw-detail-grid">
                  <div className="cw-detail-cell"><span>Revenue</span><strong>{fmt(selectedCustomer.revenue,{currency:true,compact:true})}</strong></div>
                  <div className="cw-detail-cell"><span>Margin</span><strong>{fmt(selectedCustomer.margin,{currency:true,compact:true})}</strong></div>
                  <div className="cw-detail-cell"><span>Orders</span><strong>{fmt(selectedCustomer.orders,{compact:true})}</strong></div>
                  <div className="cw-detail-cell"><span>Margin rate</span><strong>{selectedCustomer.revenue ? fmt(selectedCustomer.margin / selectedCustomer.revenue,{pct:true,ratio:true}) : '—'}</strong></div>
                  <div className="cw-detail-cell"><span>Last order</span><strong>{selectedCustomer.last_order || '—'}</strong></div>
                  <div className="cw-detail-cell"><span>Customer ID</span><strong>#{selectedCustomer.customer_id}</strong></div>
                </div>
              </div>
            ) : <div className="cw-empty">Select a customer from the portfolio table.</div>
          ) : (
            <DataTable
              viz={customerViz}
              rows={visibleCustomers}
              columns={[
                {field:'customer',headerName:'Customer'},
                {field:'region',headerName:'Region'},
                {field:'segment',headerName:'Segment'},
                {field:'orders',headerName:'Orders'},
                {field:'revenue',headerName:'Revenue'},
                {field:'margin',headerName:'Margin'},
                {field:'last_order',headerName:'Last order'}
              ]}
              pageSize={8}
              density="compact"
              searchable={false}
              sortable
              selectable
              onRowClick={row => { setSelectedId(row.customer_id); setView('chart'); }}
              format={(value, col) => ['revenue','margin'].includes(col.field) ? fmt(value,{currency:true,compact:true}) : String(value ?? '—')}
              maxHeight={360}
            />
          )}
        </section>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>