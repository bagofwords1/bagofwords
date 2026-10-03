<script type="text/babel">
setTheme('slate', {
  accent: '#5967a8',
  accent2: '#7b86bd',
  chart: ['#5967a8', '#7b86bd', '#8ea3c7', '#6e9b91', '#c79b62', '#a77b94', '#6d879b', '#9b9aa7'],
  radius: 'crisp',
  shadow: 'soft'
});

const REP_VIZ_ID = 'FIXTURE_VIZ_0';
const WEEKLY_VIZ_ID = 'FIXTURE_VIZ_1';
const PRIORITY_VIZ_ID = 'FIXTURE_VIZ_2';

function App() {
  const theme = useTheme();
  const data = useArtifactData();
  const currentUser = useCurrentUser();
  const params = useParams();

  const regionOptions = useParamOptions('region') || [];
  const channelOptions = useParamOptions('channels') || [];

  const [activeTab, setActiveTab] = useState('representatives');
  const [search, setSearch] = useState('');
  const [selectedRep, setSelectedRep] = useState(null);

  const representativeViz = vizById(REP_VIZ_ID);
  const weeklyViz = vizById(WEEKLY_VIZ_ID);
  const priorityViz = vizById(PRIORITY_VIZ_ID);

  const normalizedRegionOptions = useMemo(
    () => regionOptions.map(option => typeof option === 'string'
      ? { value: option, label: option }
      : { value: option.value, label: option.label || option.value }),
    [regionOptions]
  );

  const normalizedChannelOptions = useMemo(
    () => channelOptions.map(option => typeof option === 'string'
      ? { value: option, label: option }
      : { value: option.value, label: option.label || option.value }),
    [channelOptions]
  );

  const representativeRows = useMemo(() => {
    const rows = representativeViz?.rows || [];
    const grouped = new Map();

    rows.forEach(row => {
      const name = String(row.representative ?? 'Unassigned');
      const existing = grouped.get(name) || {
        representative: name,
        regions: new Set(),
        customers: 0,
        orders: 0,
        revenue: 0,
        margin: 0
      };

      if (row.region) existing.regions.add(row.region);
      existing.customers += Number(row.customers ?? 0);
      existing.orders += Number(row.orders ?? 0);
      existing.revenue += Number(row.revenue ?? 0);
      existing.margin += Number(row.margin ?? 0);
      grouped.set(name, existing);
    });

    return Array.from(grouped.values())
      .map(row => ({
        ...row,
        regions: Array.from(row.regions),
        marginRate: row.revenue ? row.margin / row.revenue : null,
        averageOrder: row.orders ? row.revenue / row.orders : null
      }))
      .sort((a, b) => b.revenue - a.revenue);
  }, [representativeViz?.rows]);

  const visibleRepresentatives = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return representativeRows;
    return representativeRows.filter(row =>
      row.representative.toLowerCase().includes(query) ||
      row.regions.some(region => region.toLowerCase().includes(query))
    );
  }, [representativeRows, search]);

  useEffect(() => {
    if (selectedRep && !representativeRows.some(row => row.representative === selectedRep)) {
      setSelectedRep(null);
    }
  }, [representativeRows, selectedRep]);

  useEffect(() => {
    if (!selectedRep && representativeRows.length) {
      setSelectedRep(representativeRows[0].representative);
    }
  }, [representativeRows, selectedRep]);

  const selectedRepresentative = useMemo(
    () => representativeRows.find(row => row.representative === selectedRep) || null,
    [representativeRows, selectedRep]
  );

  const selectedAccounts = useMemo(() => {
    const rows = priorityViz?.rows || [];
    return selectedRep
      ? rows.filter(row => String(row.representative ?? '') === selectedRep)
        .sort((a, b) => Number(b.revenue ?? 0) - Number(a.revenue ?? 0))
      : [];
  }, [priorityViz?.rows, selectedRep]);

  const weeklySeries = useMemo(() => {
    const rows = weeklyViz?.rows || [];
    const weeks = Array.from(new Set(rows.map(row => row.week))).sort();
    const representatives = selectedRep
      ? [selectedRep]
      : Array.from(new Set(rows.map(row => row.representative))).sort();

    return {
      weeks,
      representatives,
      series: representatives.map((representative, index) => ({
        name: representative,
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        data: weeks.map(week => {
          const match = rows.find(row =>
            row.week === week && row.representative === representative
          );
          return match ? Number(match.revenue ?? 0) : null;
        }),
        lineStyle: { width: selectedRep === representative ? 3 : 1.5 },
        itemStyle: { color: theme.colors.chart[index % theme.colors.chart.length] }
      }))
    };
  }, [weeklyViz?.rows, selectedRep, theme.colors.chart]);

  const period = params.values?.period || {};
  const selectedChannels = Array.isArray(params.values?.channels)
    ? params.values.channels
    : normalizedChannelOptions.map(option => option.value);

  const setPeriodValue = (key, value) => {
    params.setParam('period', {
      from: key === 'from' ? value : (period.from ?? null),
      to: key === 'to' ? value : (period.to ?? null)
    });
  };

  const toggleChannel = channel => {
    const next = selectedChannels.includes(channel)
      ? selectedChannels.filter(value => value !== channel)
      : [...selectedChannels, channel];
    params.setParam('channels', next);
  };

  if (!data) {
    return (
      <div className="ro-app min-h-screen bg-bg text-ink">
        <style>{styles}</style>
        <div className="ro-loading"><LoadingSpinner size={28} /><span>Loading revenue operations data…</span></div>
      </div>
    );
  }

  return (
    <div className="ro-app min-h-screen bg-bg text-ink">
      <style>{styles}</style>

      <header className="ro-header">
        <div>
          <div className="ro-eyebrow">Revenue operations</div>
          <h1>Revenue Operations</h1>
          <p>Inspect representative performance, account concentration, and weekly momentum.</p>
        </div>
        <div className="ro-header-meta">
          <span className="ro-scope-dot" />
          <span>{currentUser?.name ? `Viewing as ${currentUser.name}` : 'Workspace view'}</span>
        </div>
      </header>

      <section className="ro-control-panel" aria-label="Revenue operation filters">
        <div className="ro-control-heading">
          <div>
            <div className="ro-eyebrow">Query controls</div>
            <strong>Performance scope</strong>
          </div>
          {params.loading && <span className="ro-inline-status"><LoadingSpinner size={14} /> Updating</span>}
          {params.error && <span className="ro-error-text"><Icon name="alert-triangle" size={14} /> {String(params.error)}</span>}
        </div>

        <div className="ro-controls">
          <label className="ro-field">
            <span>Region</span>
            <select
              value={params.values?.region ?? ''}
              onChange={event => params.setParam('region', event.target.value || null)}
            >
              <option value="">All regions</option>
              {normalizedRegionOptions.map(option => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>

          <div className="ro-field ro-channel-field">
            <span>Channels</span>
            <div className="ro-channel-list">
              {normalizedChannelOptions.map(option => (
                <button
                  type="button"
                  key={option.value}
                  className={`ro-channel-chip ${selectedChannels.includes(option.value) ? 'is-selected' : ''}`}
                  aria-pressed={selectedChannels.includes(option.value)}
                  onClick={() => toggleChannel(option.value)}
                >
                  <span className="ro-check">{selectedChannels.includes(option.value) ? '✓' : ''}</span>
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          <label className="ro-field">
            <span>Period from</span>
            <input
              type="date"
              value={period.from ?? ''}
              onChange={event => setPeriodValue('from', event.target.value)}
            />
          </label>

          <label className="ro-field">
            <span>Period to</span>
            <input
              type="date"
              value={period.to ?? ''}
              onChange={event => setPeriodValue('to', event.target.value)}
            />
          </label>

          <label className="ro-field ro-number-field">
            <span>Minimum order value</span>
            <input
              type="number"
              min="0"
              step="1"
              value={params.values?.min_order ?? 0}
              onChange={event => {
                const value = event.target.value;
                params.setParam('min_order', value === '' ? null : Number(value));
              }}
            />
          </label>
        </div>
      </section>

      <nav className="ro-tabs" aria-label="Revenue operations views">
        <button
          type="button"
          className={activeTab === 'representatives' ? 'is-active' : ''}
          onClick={() => setActiveTab('representatives')}
        >
          <Icon name="users" size={16} /> Representatives
        </button>
        <button
          type="button"
          className={activeTab === 'weekly' ? 'is-active' : ''}
          onClick={() => setActiveTab('weekly')}
        >
          <Icon name="activity" size={16} /> Weekly view
        </button>
      </nav>

      {activeTab === 'representatives' ? (
        <main className="ro-workspace">
          <section className="ro-list-panel" data-bow-viz={REP_VIZ_ID}>
            <div className="ro-section-head">
              <div>
                <div className="ro-eyebrow">Ranked performance</div>
                <h2>Representatives</h2>
              </div>
              <span className="ro-count">{visibleRepresentatives.length} shown</span>
            </div>

            <div className="ro-search">
              <Icon name="search" size={16} />
              <input
                aria-label="Search representatives"
                placeholder="Search name or region"
                value={search}
                onChange={event => setSearch(event.target.value)}
              />
              {search && (
                <button type="button" aria-label="Clear search" onClick={() => setSearch('')}>
                  <Icon name="x" size={15} />
                </button>
              )}
            </div>

            {visibleRepresentatives.length ? (
              <div className="ro-rep-list">
                {visibleRepresentatives.map((row, index) => (
                  <button
                    type="button"
                    key={row.representative}
                    className={`ro-rep-row ${selectedRep === row.representative ? 'is-selected' : ''}`}
                    onClick={() => setSelectedRep(row.representative)}
                    aria-pressed={selectedRep === row.representative}
                  >
                    <span className="ro-rank">{String(index + 1).padStart(2, '0')}</span>
                    <span className="ro-rep-identity">
                      <strong>{row.representative}</strong>
                      <small>{row.regions.join(' · ') || 'Region unavailable'}</small>
                    </span>
                    <span className="ro-rep-revenue">
                      <strong>{fmt(row.revenue, { currency: true, compact: true })}</strong>
                      <small>revenue</small>
                    </span>
                    <span className="ro-rep-margin">
                      <Badge tone={row.marginRate >= 0.42 ? 'positive' : row.marginRate >= 0.35 ? 'warning' : 'negative'}>
                        {row.marginRate == null ? '—' : share(row.margin, row.revenue)}
                      </Badge>
                    </span>
                    <Icon name="chevron-right" size={17} />
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState icon="search">No representatives match this search.</EmptyState>
            )}
          </section>

          <aside className="ro-detail-panel">
            {selectedRepresentative ? (
              <>
                <div className="ro-detail-heading" data-bow-viz={REP_VIZ_ID} data-bow-calc="GROUP BY representative; SUM(revenue, margin, customers, orders)">
                  <div>
                    <div className="ro-eyebrow">Representative detail</div>
                    <h2>{selectedRepresentative.representative}</h2>
                    <p>{selectedRepresentative.regions.join(' · ') || 'Region unavailable'}</p>
                  </div>
                  <Badge tone="accent">Selected</Badge>
                </div>

                <div className="ro-kpi-grid">
                  <div className="ro-kpi" data-bow-viz={REP_VIZ_ID} data-bow-calc="SUM(revenue)">
                    <span>Revenue</span>
                    <strong>{fmt(selectedRepresentative.revenue, { currency: true, compact: true })}</strong>
                  </div>
                  <div className="ro-kpi" data-bow-viz={REP_VIZ_ID} data-bow-calc="SUM(margin)">
                    <span>Margin</span>
                    <strong>{fmt(selectedRepresentative.margin, { currency: true, compact: true })}</strong>
                  </div>
                  <div className="ro-kpi" data-bow-viz={REP_VIZ_ID} data-bow-calc="SUM(customers)">
                    <span>Customers</span>
                    <strong>{fmt(selectedRepresentative.customers, { compact: true })}</strong>
                  </div>
                  <div className="ro-kpi" data-bow-viz={REP_VIZ_ID} data-bow-calc="SUM(orders)">
                    <span>Orders</span>
                    <strong>{fmt(selectedRepresentative.orders, { compact: true })}</strong>
                  </div>
                </div>

                <section className="ro-detail-section" data-bow-viz={PRIORITY_VIZ_ID} data-bow-calc="FILTER representative = selected representative; ORDER BY revenue DESC">
                  <div className="ro-section-head compact">
                    <div>
                      <div className="ro-eyebrow">Account concentration</div>
                      <h3>Priority accounts</h3>
                    </div>
                    <span className="ro-count">{selectedAccounts.length} accounts</span>
                  </div>

                  {selectedAccounts.length ? (
                    <DataTable
                      viz={priorityViz}
                      rows={selectedAccounts}
                      columns={[
                        { field: 'customer', headerName: 'Customer' },
                        { field: 'orders', headerName: 'Orders' },
                        { field: 'revenue', headerName: 'Revenue' }
                      ]}
                      pageSize={5}
                      density="compact"
                      sortable
                      maxHeight={300}
                      format={(value, column) =>
                        column.field === 'revenue'
                          ? fmt(value, { currency: true, compact: true })
                          : fmt(value, { compact: true })
                      }
                    />
                  ) : (
                    <EmptyState icon="inbox">No priority accounts for this representative.</EmptyState>
                  )}
                </section>

                <section className="ro-detail-section ro-snapshot" data-bow-viz={REP_VIZ_ID} data-bow-calc="SUM(margin) / SUM(revenue)">
                  <div className="ro-section-head compact">
                    <div>
                      <div className="ro-eyebrow">Efficiency snapshot</div>
                      <h3>Commercial quality</h3>
                    </div>
                  </div>
                  <div className="ro-quality-row">
                    <div>
                      <span>Margin rate</span>
                      <strong>{selectedRepresentative.marginRate == null ? '—' : share(selectedRepresentative.margin, selectedRepresentative.revenue)}</strong>
                    </div>
                    <div>
                      <span>Average order</span>
                      <strong>{selectedRepresentative.averageOrder == null ? '—' : fmt(selectedRepresentative.averageOrder, { currency: true, compact: true })}</strong>
                    </div>
                  </div>
                  <ProgressBar
                    value={selectedRepresentative.marginRate == null ? 0 : Math.min(selectedRepresentative.marginRate, 1)}
                    tone={selectedRepresentative.marginRate >= 0.42 ? 'positive' : 'accent'}
                  />
                </section>
              </>
            ) : (
              <div className="ro-no-selection">
                <EmptyState icon="mouse-pointer-2">Select a representative to inspect performance.</EmptyState>
              </div>
            )}
          </aside>
        </main>
      ) : (
        <main className="ro-weekly-workspace">
          <section className="ro-chart-panel" data-bow-viz={WEEKLY_VIZ_ID} data-bow-calc="SUM(revenue) BY week, representative">
            <div className="ro-section-head">
              <div>
                <div className="ro-eyebrow">Revenue cadence</div>
                <h2>{selectedRep ? `${selectedRep} · weekly revenue` : 'Weekly revenue'}</h2>
                <p className="ro-muted">Source-backed weekly performance for the current query scope.</p>
              </div>
              {selectedRep && (
                <button type="button" className="ro-clear-selection" onClick={() => setSelectedRep(null)}>
                  <Icon name="x" size={14} /> Clear representative
                </button>
              )}
            </div>

            {weeklySeries.weeks.length ? (
              <EChart
                height={460}
                viz={weeklyViz}
                option={{
                  tooltip: { trigger: 'axis', valueFormatter: value => fmt(value, { currency: true }) },
                  legend: { show: !selectedRep, bottom: 0, type: 'scroll' },
                  grid: { left: 58, right: 24, top: 24, bottom: selectedRep ? 24 : 54 },
                  xAxis: { type: 'category', data: weeklySeries.weeks, boundaryGap: false },
                  yAxis: {
                    type: 'value',
                    axisLabel: { formatter: value => fmt(value, { currency: true, compact: true }) }
                  },
                  series: weeklySeries.series
                }}
              />
            ) : (
              <EmptyState icon="bar-chart-3">No weekly performance is available for this scope.</EmptyState>
            )}
          </section>
        </main>
      )}

      <footer className="ro-footer">
        <span><Icon name="database" size={14} /> Source-backed workspace</span>
        <span>Filters rerun the connected performance queries; search only filters loaded representatives.</span>
      </footer>
    </div>
  );
}

const styles = `
.ro-app {
  --ro-line: color-mix(in srgb, var(--bow-line) 82%, transparent);
  --ro-line-soft: color-mix(in srgb, var(--bow-line) 52%, transparent);
  --ro-surface: color-mix(in srgb, var(--bow-surface) 78%, var(--bow-bg));
  font-family: var(--bow-font-body);
  color: var(--bow-ink);
}
.ro-app * { box-sizing: border-box; }
.ro-header, .ro-control-panel, .ro-tabs, .ro-workspace, .ro-weekly-workspace, .ro-footer {
  width: min(1400px, calc(100% - 48px));
  margin-left: auto;
  margin-right: auto;
}
.ro-header {
  padding: 38px 0 26px;
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: flex-end;
}
.ro-header h1, .ro-section-head h2, .ro-detail-heading h2, .ro-section-head h3 {
  margin: 0;
  letter-spacing: -0.035em;
}
.ro-header h1 { font-family: var(--bow-font-display); font-size: clamp(30px, 4vw, 46px); font-weight: 650; }
.ro-header p, .ro-detail-heading p, .ro-muted { color: var(--bow-ink-2); margin: 8px 0 0; font-size: 13px; }
.ro-eyebrow {
  color: var(--bow-accent);
  font-size: 10px;
  font-weight: 750;
  letter-spacing: .14em;
  text-transform: uppercase;
  margin-bottom: 9px;
}
.ro-header-meta, .ro-inline-status, .ro-footer {
  color: var(--bow-ink-3);
  font-size: 11px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.ro-scope-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--bow-positive); }
.ro-control-panel {
  padding: 17px 18px 18px;
  background: var(--ro-surface);
  border: 1px solid var(--ro-line);
  border-radius: 10px;
}
.ro-control-heading, .ro-section-head, .ro-detail-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
.ro-control-heading strong { font-size: 14px; }
.ro-controls {
  display: grid;
  grid-template-columns: 1fr 1.55fr 1fr 1fr 1.1fr;
  gap: 13px;
  margin-top: 16px;
}
.ro-field { display: flex; flex-direction: column; gap: 7px; min-width: 0; }
.ro-field > span { font-size: 11px; color: var(--bow-ink-2); font-weight: 650; }
.ro-field input, .ro-field select, .ro-search input {
  height: 36px;
  border: 1px solid var(--ro-line);
  background: var(--bow-surface);
  color: var(--bow-ink);
  border-radius: 6px;
  padding: 0 10px;
  font: inherit;
  font-size: 12px;
  outline: none;
}
.ro-field input:focus, .ro-field select:focus, .ro-search:focus-within {
  border-color: var(--bow-accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--bow-accent) 14%, transparent);
}
.ro-channel-list { display: flex; flex-wrap: wrap; gap: 5px; min-height: 36px; align-items: center; }
.ro-channel-chip {
  border: 1px solid var(--ro-line);
  color: var(--bow-ink-2);
  background: var(--bow-surface);
  border-radius: 5px;
  padding: 7px 8px;
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}
.ro-channel-chip.is-selected { color: var(--bow-accent); border-color: color-mix(in srgb, var(--bow-accent) 55%, var(--ro-line)); background: color-mix(in srgb, var(--bow-accent) 9%, var(--bow-surface)); }
.ro-check { display: inline-block; width: 12px; font-weight: 800; }
.ro-error-text { color: var(--bow-negative); font-size: 11px; display: flex; gap: 5px; align-items: center; }
.ro-tabs {
  display: flex;
  gap: 24px;
  border-bottom: 1px solid var(--ro-line);
  margin-top: 28px;
}
.ro-tabs button {
  border: 0;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--bow-ink-3);
  padding: 12px 2px 11px;
  font: inherit;
  font-size: 12px;
  font-weight: 650;
  display: flex;
  gap: 7px;
  cursor: pointer;
}
.ro-tabs button.is-active { color: var(--bow-accent); border-bottom-color: var(--bow-accent); }
.ro-workspace { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(360px, .85fr); gap: 18px; padding: 22px 0 40px; }
.ro-list-panel, .ro-detail-panel, .ro-chart-panel {
  background: var(--ro-surface);
  border: 1px solid var(--ro-line);
  border-radius: 10px;
}
.ro-list-panel { padding: 20px; }
.ro-detail-panel { padding: 22px; min-width: 0; }
.ro-section-head h2 { font-size: 21px; }
.ro-section-head h3 { font-size: 15px; }
.ro-section-head.compact { align-items: center; }
.ro-count { color: var(--bow-ink-3); font-size: 11px; white-space: nowrap; }
.ro-search {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--ro-line);
  border-radius: 6px;
  background: var(--bow-surface);
  padding: 0 9px;
  margin: 19px 0 12px;
  color: var(--bow-ink-3);
}
.ro-search input { border: 0; padding: 0; width: 100%; box-shadow: none; }
.ro-search button, .ro-clear-selection {
  border: 0;
  background: none;
  color: var(--bow-ink-3);
  cursor: pointer;
}
.ro-rep-list { border-top: 1px solid var(--ro-line); }
.ro-rep-row {
  display: grid;
  grid-template-columns: 30px minmax(120px, 1fr) 105px 70px 17px;
  align-items: center;
  gap: 10px;
  width: 100%;
  text-align: left;
  border: 0;
  border-bottom: 1px solid var(--ro-line-soft);
  background: transparent;
  color: var(--bow-ink);
  padding: 14px 4px;
  cursor: pointer;
}
.ro-rep-row:hover, .ro-rep-row.is-selected { background: color-mix(in srgb, var(--bow-accent) 7%, transparent); }
.ro-rep-row.is-selected { box-shadow: inset 3px 0 var(--bow-accent); }
.ro-rank { color: var(--bow-ink-3); font: 11px var(--bow-font-mono); }
.ro-rep-identity, .ro-rep-revenue { display: flex; flex-direction: column; min-width: 0; }
.ro-rep-identity strong, .ro-rep-revenue strong { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ro-rep-identity small, .ro-rep-revenue small { color: var(--bow-ink-3); font-size: 10px; margin-top: 4px; }
.ro-rep-revenue { text-align: right; }
.ro-rep-margin { justify-self: end; }
.ro-detail-heading { padding-bottom: 20px; border-bottom: 1px solid var(--ro-line); }
.ro-detail-heading h2 { font-size: 26px; font-family: var(--bow-font-display); }
.ro-kpi-grid { display: grid; grid-template-columns: repeat(2, 1fr); border-bottom: 1px solid var(--ro-line); padding: 17px 0; gap: 16px 12px; }
.ro-kpi { display: flex; flex-direction: column; gap: 5px; }
.ro-kpi span, .ro-quality-row span { color: var(--bow-ink-3); font-size: 10px; text-transform: uppercase; letter-spacing: .08em; }
.ro-kpi strong { font: 650 20px var(--bow-font-numeric); letter-spacing: -.03em; }
.ro-detail-section { padding-top: 21px; }
.ro-snapshot { border-top: 1px solid var(--ro-line); margin-top: 21px; }
.ro-quality-row { display: flex; justify-content: space-between; gap: 20px; margin: 17px 0 13px; }
.ro-quality-row div { display: flex; flex-direction: column; gap: 5px; }
.ro-quality-row strong { font: 650 17px var(--bow-font-numeric); }
.ro-no-selection { min-height: 320px; display: grid; place-items: center; }
.ro-weekly-workspace { padding: 22px 0 40px; }
.ro-chart-panel { padding: 22px; }
.ro-clear-selection { font: inherit; font-size: 11px; display: flex; gap: 5px; align-items: center; }
.ro-footer { padding: 0 0 28px; justify-content: space-between; }
.ro-footer span { display: flex; gap: 6px; align-items: center; }
.ro-loading { min-height: 70vh; display: flex; align-items: center; justify-content: center; gap: 10px; color: var(--bow-ink-2); font-size: 13px; }

@media (max-width: 900px) {
  .ro-controls { grid-template-columns: repeat(3, 1fr); }
  .ro-workspace { grid-template-columns: 1fr; }
}
@media (max-width: 620px) {
  .ro-header, .ro-control-panel, .ro-tabs, .ro-workspace, .ro-weekly-workspace, .ro-footer { width: min(100% - 24px, 1400px); }
  .ro-header { padding-top: 24px; display: block; }
  .ro-header-meta { margin-top: 17px; }
  .ro-controls { grid-template-columns: 1fr 1fr; }
  .ro-channel-field { grid-column: 1 / -1; }
  .ro-rep-row { grid-template-columns: 25px minmax(110px, 1fr) 88px 17px; }
  .ro-rep-margin { display: none; }
  .ro-list-panel, .ro-detail-panel, .ro-chart-panel { padding: 15px; }
  .ro-footer { display: block; line-height: 1.6; }
  .ro-footer span + span { margin-top: 7px; }
}
@media (max-width: 410px) {
  .ro-controls { grid-template-columns: 1fr; }
  .ro-channel-field { grid-column: auto; }
  .ro-rep-row { grid-template-columns: 24px minmax(100px, 1fr) 78px 14px; gap: 7px; }
  .ro-rep-revenue strong { font-size: 11px; }
}
`;

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>