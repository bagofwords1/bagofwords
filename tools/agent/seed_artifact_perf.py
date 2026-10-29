#!/usr/bin/env python3
"""Seed complex artifact dashboards for the artifact load-performance loop
(docs/feedback-loops/artifact-load-performance.md).

Run against the live stack (tools/agent/boot_stack.sh + seed_org.py with
--invite viewer@example.com) with the backend's env so the models bind the
same sqlite DB:

    cd backend && TESTING=true ENVIRONMENT=production \
      TEST_DATABASE_URL=sqlite:///db/agent.db \
      uv run python ../tools/agent/seed_artifact_perf.py > /tmp/perf_seed.json

Every step's code is deterministic and produces exactly the rows seeded into
Step.data, so a refresh-on-view rerun never changes what a page renders —
before/after fingerprints stay comparable.

Reports (all owned by admin@example.com):
  exec       heavy themed page: KPIs, 3 EChart charts, 2.5k-row DataTable,
             tabs, Icon, modern JS syntax; public
  params     useParams/FilterSelect driven by an options_source query, plus an
             identity-scoped query (withheld for non-owners); public
  edge       `import` lines, class components + error boundary, generators,
             async/await, top-level let/const; a NEWER doc artifact on the
             same report (the shared page must keep the page); internal
  syntax     artifact code with a JSX syntax error (error path); public
  runtime    artifact that throws during render (error path); public
  files      embedded PNG + PDF through BowFile; public
  slides     legacy HTML slides; public
  legacy     raw echarts.init + window.lucide, no runtime_version; public

Prints JSON {name: {report_id, artifact_id, visibility}}.
"""
import asyncio
import base64
import json
import sys
import uuid
import zlib
import struct

import httpx

BASE = "http://localhost:8000"
ADMIN = {"username": "admin@example.com", "password": "Password123!"}


# ── Deterministic data generators (shared by seed and step code) ────────────
# Each is a complete `generate_df` body: the seed exec()s it to compute the
# snapshot, and the same text is the step's code, so a rerun reproduces it.

GEN_KPIS = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame([{"revenue": 12873450.5, "orders": 48211, "aov": 267.02, "churn": 0.0312, "nps": 61}])
"""

GEN_MONTHLY = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    rows = []
    for i in range(36):
        y, m = 2023 + i // 12, i % 12 + 1
        rev = 800000 + i * 21000 + ((i * 7919) % 97) * 1300
        cost = int(rev * (0.58 + ((i * 31) % 11) / 200))
        rows.append({"month": f"{y}-{m:02d}", "revenue": rev, "cost": cost, "margin": rev - cost})
    return pd.DataFrame(rows)
"""

GEN_CATEGORIES = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    cats = ["Hardware", "Software", "Services", "Support", "Training", "Cloud", "Consulting", "Other"]
    return pd.DataFrame([{"category": c, "revenue": 300000 + ((i * 104729) % 900) * 1000, "share": round(1 / (i + 2), 4)} for i, c in enumerate(cats)])
"""

GEN_ORDERS = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    regions = ["North", "South", "East", "West", "Central"]
    products = ["Widget", "Gadget", "Gizmo", "Doohickey", "Thingamajig", "Whatsit"]
    statuses = ["shipped", "pending", "returned", "cancelled"]
    rows = []
    for i in range(2500):
        rows.append({
            "order_id": f"ORD-{100000 + i}",
            "date": f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "region": regions[(i * 7) % 5],
            "product": products[(i * 13) % 6],
            "qty": (i * 17) % 23 + 1,
            "price": round(9.99 + ((i * 37) % 400) / 3, 2),
            "status": statuses[(i * 11) % 4],
            "note": "fragile <b>&amp;</b> \\"quoted\\" — ünïcødé" if i % 97 == 0 else "",
        })
    return pd.DataFrame(rows)
"""

GEN_REGIONS = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame([{"region": r, "label": r + " region"} for r in ["North", "South", "East", "West", "Central"]])
"""

GEN_REGION_SALES = """
def generate_df(ds_clients, excel_files, params=None):
    import pandas as pd
    params = params or {}
    regions = ["North", "South", "East", "West", "Central"]
    sel = params.get("region")
    rows = []
    for ri, r in enumerate(regions):
        if sel and r != sel:
            continue
        for q in range(1, 9):
            rows.append({"region": r, "quarter": f"Q{(q - 1) % 4 + 1} {2024 + (q - 1) // 4}", "sales": 10000 + ri * 2500 + q * 900})
    return pd.DataFrame(rows)
"""

GEN_MY_ACCOUNTS = """
def generate_df(ds_clients, excel_files, params=None):
    import pandas as pd
    params = params or {}
    owner = params.get("viewer_email") or "nobody"
    return pd.DataFrame([{"account": f"Acct {i}", "owner": owner, "arr": 50000 + i * 7500} for i in range(1, 7)])
"""


def _rows_for(code: str, params: dict | None = None) -> dict:
    ns: dict = {}
    exec(code, ns)  # noqa: S102 - our own literal code
    fn = ns["generate_df"]
    try:
        df = fn(None, None, params) if params is not None else fn(None, None)
    except TypeError:
        df = fn(None, None)
    rows = json.loads(df.to_json(orient="records"))
    cols = [{"field": c, "headerName": c} for c in df.columns]
    return {"rows": rows, "columns": cols}


# ── Artifact code ───────────────────────────────────────────────────────────

EXEC_CODE = r"""<script type="text/babel">
const fmtMoney = (v, digits = 0) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: digits }).format(v ?? 0);
const fmtPct = (v) => `${((v ?? 0) * 100).toFixed(1)}%`;
let renderCount = 0;

function* monthPairs(rows) {
  for (let i = 1; i < rows.length; i++) yield [rows[i - 1], rows[i]];
}

class Stats {
  static of(values) { return new Stats(values); }
  #values;
  constructor(values) { this.#values = [...values]; }
  get sum() { return this.#values.reduce((a, b) => a + b, 0); }
  get mean() { return this.#values.length ? this.sum / this.#values.length : 0; }
  get max() { return Math.max(...this.#values); }
}

function App() {
  const data = useArtifactData();
  const [tab, setTab] = useState('trend');
  const [ready, setReady] = useState(false);
  renderCount += 1;

  useEffect(() => {
    let alive = true;
    (async () => {
      await new Promise((r) => setTimeout(r, 30));
      if (alive) setReady(true);
    })();
    return () => { alive = false; };
  }, []);

  const viz = data?.visualizations ?? [];
  const [kpis, monthly, cats, orders] = [0, 1, 2, 3].map((i) => viz[i] ?? { rows: [] });
  const k = kpis.rows?.[0] ?? {};

  const growth = useMemo(() => {
    const out = [];
    for (const [a, b] of monthPairs(monthly.rows ?? [])) out.push({ month: b.month, pct: (b.revenue - a.revenue) / a.revenue });
    return out;
  }, [monthly.rows]);

  const byRegion = useMemo(() => {
    const m = new Map();
    for (const { region, qty, price } of orders.rows ?? []) m.set(region, (m.get(region) ?? 0) + qty * price);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [orders.rows]);

  const revStats = Stats.of((monthly.rows ?? []).map((r) => r.revenue));
  const statusSet = new Set((orders.rows ?? []).map((r) => r.status));

  const lineOption = {
    tooltip: { trigger: 'axis' },
    legend: { data: ['Revenue', 'Cost', 'Margin'] },
    xAxis: { type: 'category', data: (monthly.rows ?? []).map((r) => r.month) },
    yAxis: { type: 'value' },
    animation: false,
    series: [
      { name: 'Revenue', type: 'line', smooth: true, data: (monthly.rows ?? []).map((r) => r.revenue) },
      { name: 'Cost', type: 'line', data: (monthly.rows ?? []).map((r) => r.cost) },
      { name: 'Margin', type: 'bar', data: (monthly.rows ?? []).map((r) => r.margin) },
    ],
  };
  const pieOption = {
    animation: false,
    tooltip: { trigger: 'item' },
    series: [{ type: 'pie', radius: ['40%', '70%'], data: (cats.rows ?? []).map(({ category: name, revenue: value }) => ({ name, value })) }],
  };
  const barOption = {
    animation: false,
    xAxis: { type: 'category', data: byRegion.map(([r]) => r) },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: byRegion.map(([, v]) => Math.round(v)) }],
  };

  return (
    <div className="min-h-full p-6 space-y-6" data-testid="exec-root">
      <PageHeader title="Executive Overview" subtitle={`FY 2023–2025 · ${statusSet.size} order statuses · render ${renderCount > 0 ? 'ok' : 'none'}`} />
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <KPICard title="Revenue" value={fmtMoney(k.revenue)} subtitle="trailing 12m" />
        <KPICard title="Orders" value={(k.orders ?? 0).toLocaleString('en-US')} />
        <KPICard title="AOV" value={fmtMoney(k.aov, 2)} />
        <KPICard title="Churn" value={fmtPct(k.churn)} />
        <KPICard title="NPS" value={String(k.nps ?? '—')} />
      </div>
      <div className="flex items-center gap-2 text-sm">
        <Icon name="trending-up" size={16} />
        <span data-testid="rev-stats">mean {fmtMoney(revStats.mean)} · max {fmtMoney(revStats.max)} · last growth {fmtPct(growth.at(-1)?.pct)}</span>
        <span className={ready ? 'text-green-600' : 'text-gray-400'} data-testid="async-ready">{ready ? 'async ready' : 'async pending'}</span>
      </div>
      <Segmented options={[{ value: 'trend', label: 'Trend' }, { value: 'mix', label: 'Mix' }, { value: 'region', label: 'Regions' }]} value={tab} onChange={setTab} />
      <SectionCard title={tab === 'trend' ? 'Revenue trend' : tab === 'mix' ? 'Category mix' : 'Sales by region'}>
        {tab === 'trend' && <EChart option={lineOption} height={320} />}
        {tab === 'mix' && <EChart option={pieOption} height={320} />}
        {tab === 'region' && <EChart option={barOption} height={320} />}
      </SectionCard>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <SectionCard title="Category mix"><EChart option={pieOption} height={260} /></SectionCard>
        <SectionCard title="Regions"><EChart option={barOption} height={260} /></SectionCard>
      </div>
      <SectionCard title={`Orders (${(orders.rows ?? []).length.toLocaleString('en-US')})`}>
        <DataTable rows={orders.rows ?? []} columns={orders.columns ?? []} pageSize={25} searchable sortable />
      </SectionCard>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

PARAMS_CODE = r"""<script type="text/babel">
function App() {
  const data = useArtifactData();
  const { values, setParam, apply, loading, declarations } = useParams();
  const regionOpts = useParamOptions('region');
  const viz = data?.visualizations ?? [];
  const sales = viz.find((v) => v.title === 'Sales by region') ?? viz[0] ?? { rows: [] };
  const mine = viz.find((v) => v.title === 'My accounts') ?? { rows: [] };
  const total = (sales.rows ?? []).reduce((a, r) => a + (r.sales || 0), 0);
  const user = useCurrentUser();
  return (
    <div className="p-6 space-y-4" data-testid="params-root">
      <h1 className="text-xl font-semibold">Filtered Sales</h1>
      <p className="text-xs text-gray-500" data-testid="viewer">viewer: {user?.email ?? 'anonymous'} · {declarations.length} params · {loading ? 'loading' : 'idle'}</p>
      <FilterBar>
        <FilterSelect label="Region" options={regionOpts} selected={values.region ? [values.region] : []} single onChange={(v) => { setParam('region', (v && v[0]) ?? null); apply(); }} />
      </FilterBar>
      <div className="text-2xl font-bold" data-testid="total">{total.toLocaleString('en-US')}</div>
      <EChart option={{ animation: false, xAxis: { type: 'category', data: (sales.rows ?? []).map((r) => r.region + ' ' + r.quarter) }, yAxis: { type: 'value' }, series: [{ type: 'bar', data: (sales.rows ?? []).map((r) => r.sales) }] }} height={280} />
      <h2 className="font-medium">My accounts</h2>
      {(mine.rows ?? []).length === 0
        ? <p className="text-sm text-gray-500" data-testid="mine-empty">No accounts visible to you yet.</p>
        : <DataTable rows={mine.rows} columns={mine.columns} pageSize={10} />}
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

EDGE_CODE = r"""<script type="text/babel">
import React, { useState as useLocalState } from 'react';
import { Chart } from 'some-lib';

var legacyCounter = 0;
const labels = Object.freeze({ a: 'Alpha', b: 'Beta' });
let tag = (strings, ...vals) => strings.raw.map((s, i) => s + (vals[i] ?? '')).join('');

class Boundary extends React.Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (this.state.error) return <div className="p-3 bg-red-50 text-red-700 rounded" data-testid="caught">caught: {this.state.error.message}</div>;
    return this.props.children;
  }
}

function Bomb({ armed }) {
  if (armed) throw new Error('child exploded on purpose');
  return <span>safe</span>;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function App() {
  const data = useArtifactData();
  const [n, setN] = useLocalState(0);
  const [log, setLog] = useState([]);
  legacyCounter++;
  useEffect(() => {
    (async () => {
      for await (const x of (async function* () { yield 1; await sleep(5); yield 2; })()) {
        setLog((l) => [...l, `tick ${x}`]);
      }
    })();
  }, []);
  const rows = data?.visualizations?.[0]?.rows ?? [];
  const { revenue = 0, ...rest } = rows[0] ?? {};
  return (
    <div className="p-6 space-y-3" data-testid="edge-root">
      <h1 className="text-lg font-semibold">{labels.a} / {labels.b}</h1>
      <p data-testid="tagged">{tag`raw\n${n}`}</p>
      <p data-testid="rest">{Object.keys(rest).sort().join(',')} · rev {revenue}</p>
      <p data-testid="log">{log.join(' | ')}</p>
      <button className="px-3 py-1 border rounded" onClick={() => setN((v) => v + 1)} data-testid="inc">count {n}</button>
      <Boundary><Bomb armed /></Boundary>
      <Boundary><Bomb armed={false} /></Boundary>
      <p data-testid="types">{typeof Chart} {typeof legacyCounter} {String(globalThis === window)}</p>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

SYNTAX_CODE = r"""<script type="text/babel">
function App() {
  const data = useArtifactData();
  return (
    <div className="p-6">
      <h1>Broken {data?.report?.title</h1>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

RUNTIME_CODE = r"""<script type="text/babel">
function App() {
  const data = useArtifactData();
  const rows = data.visualizations[0].rows;
  return <div className="p-6">{rows.map((r) => r.missing.deep.value)}</div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

FILES_CODE_TMPL = r"""<script type="text/babel">
function App() {
  return (
    <div className="p-6 space-y-4" data-testid="files-root">
      <h1 className="text-lg font-semibold">Embedded files</h1>
      <div className="h-40"><BowFile id="__PNG__" /></div>
      <div style={{ height: 420 }}><BowFile id="__PDF__" /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

SLIDES_CODE = """<div class="slide h-screen flex items-center justify-center text-white" data-testid="slide1">
  <div class="text-center"><h1 class="text-5xl font-bold">Quarterly Review</h1><p class="mt-4 text-slate-300">HTML slides artifact</p></div>
</div>
<script>window.__slideData = (window.ARTIFACT_DATA && window.ARTIFACT_DATA.visualizations || []).length;</script>"""

LEGACY_CODE = r"""<script type="text/babel">
function Chart({ rows }) {
  const ref = useRef(null);
  useEffect(() => {
    const c = echarts.init(ref.current);
    c.setOption({ animation: false, xAxis: { type: 'category', data: rows.map((r) => r.month) }, yAxis: { type: 'value' }, series: [{ type: 'line', data: rows.map((r) => r.revenue) }] });
    return () => c.dispose();
  }, [rows]);
  return <div ref={ref} style={{ height: 300 }} data-testid="legacy-chart" />;
}
function App() {
  const data = useArtifactData();
  useEffect(() => { if (window.lucide) window.lucide.createIcons(); });
  const rows = data?.visualizations?.[0]?.rows || [];
  return (
    <div className="p-8 bg-gradient-to-br from-indigo-50 to-white min-h-full">
      <h1 className="text-2xl font-bold flex items-center gap-2"><i data-lucide="bar-chart-3"></i>Legacy revenue</h1>
      <Chart rows={rows} />
      <ul className="mt-4 text-sm grid grid-cols-6 gap-1">{rows.slice(0, 12).map((r) => <li key={r.month} className="bg-white rounded shadow-sm px-2 py-1">{r.month}</li>)}</ul>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

DOC_MARKDOWN = "# Notes\n\nA doc saved after the dashboard. The shared page must still open the dashboard.\n"


def _png_bytes(w=64, h=32) -> bytes:
    raw = b"".join(b"\x00" + bytes([(x * 4) % 256, (y * 8) % 256, 160] * 1)[:3] * 1 for y in range(h) for x in range(1))
    # simple gradient rows
    raw = b"".join(b"\x00" + b"".join(bytes([(x * 4) % 256, (y * 8) % 256, 160]) for x in range(w)) for y in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _pdf_bytes() -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 24 Tf 40 100 Td (Embedded PDF page) Tj ET"
    objs[3] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


# ── Seeding ─────────────────────────────────────────────────────────────────

def api():
    client = httpx.Client(base_url=BASE, timeout=60)
    r = client.post("/api/auth/jwt/login", data=ADMIN)
    r.raise_for_status()
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    org_id = client.get("/api/organizations", headers=headers).json()[0]["id"]
    headers["X-Organization-Id"] = org_id
    return client, headers


def new_report(client, headers, title):
    r = client.post("/api/reports", json={"title": title, "widget": None, "files": [], "data_sources": []}, headers=headers)
    r.raise_for_status()
    return r.json()["id"]


async def add_queries(report_id: str, specs: list[dict]) -> list[str]:
    """specs: [{title, code, parameters?, params_for_snapshot?}] → viz ids in order."""
    import main  # noqa: F401  - full model registry, like the server boot
    from app.dependencies import async_session_maker
    from app.models.query import Query
    from app.models.report import Report
    from app.models.step import Step
    from app.models.visualization import Visualization
    from app.models.widget import Widget

    viz_ids = []
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id
        for spec in specs:
            sfx = uuid.uuid4().hex[:8]
            widget = Widget(title=spec["title"], slug=f"w-{sfx}", report_id=report_id)
            db.add(widget)
            await db.flush()
            q = Query(title=spec["title"], report_id=report_id, widget_id=widget.id,
                      organization_id=org_id, user_id=user_id, parameters=spec.get("parameters"))
            db.add(q)
            await db.flush()
            data = _rows_for(spec["code"], spec.get("params_for_snapshot"))
            step = Step(title=spec["title"], slug=f"step-{sfx}", status="success", widget_id=widget.id,
                        query_id=q.id, code=spec["code"], data=data,
                        applied_params=spec.get("params_for_snapshot"))
            db.add(step)
            await db.flush()
            q.default_step_id = step.id
            viz = Visualization(title=spec["title"], status="success", report_id=report_id,
                                query_id=q.id, view={"type": spec.get("view", "table")})
            db.add(viz)
            await db.flush()
            viz_ids.append(str(viz.id))
            spec["_query_id"] = str(q.id)
        await db.commit()
    return viz_ids


def create_artifact(client, headers, report_id, title, mode, content):
    r = client.post("/api/artifacts", json={"report_id": report_id, "title": title, "mode": mode, "content": content}, headers=headers)
    r.raise_for_status()
    return r.json()


def set_visibility(client, headers, report_id, vis):
    r = client.put(f"/api/reports/{report_id}/visibility/artifact", json={"visibility": vis}, headers=headers)
    r.raise_for_status()


def upload(client, headers, name, content, ctype):
    r = client.post("/api/files", files={"file": (name, content, ctype)}, headers=headers)
    r.raise_for_status()
    return r.json()


def main():
    client, headers = api()
    out = {}

    def report(name, title, specs, artifacts, vis):
        rid = new_report(client, headers, title)
        viz = asyncio.run(add_queries(rid, specs)) if specs else []
        last = None
        for a in artifacts:
            content = dict(a["content"])
            if a["mode"] == "page" and "visualization_ids" not in content:
                content["visualization_ids"] = viz
            last = create_artifact(client, headers, rid, a["title"], a["mode"], content)
            if a.get("primary"):
                primary = last
        primary = locals().get("primary") or last
        set_visibility(client, headers, rid, vis)
        out[name] = {"report_id": rid, "artifact_id": primary["id"], "visibility": vis,
                     "query_ids": [s.get("_query_id") for s in specs]}

    report("exec", "Executive Overview", [
        {"title": "KPIs", "code": GEN_KPIS},
        {"title": "Monthly", "code": GEN_MONTHLY},
        {"title": "Categories", "code": GEN_CATEGORIES},
        {"title": "Orders", "code": GEN_ORDERS},
    ], [{"title": "Executive Overview", "mode": "page", "content": {"code": EXEC_CODE, "runtime_version": 11}}], "public")

    regions = {"title": "Regions", "code": GEN_REGIONS}
    sales = {"title": "Sales by region", "code": GEN_REGION_SALES, "params_for_snapshot": {},
             "parameters": [{"name": "region", "type": "string", "label": "Region", "source": "input",
                             "options_source": {"query_id": "Regions", "value_column": "region", "label_column": "label"}}]}
    mine = {"title": "My accounts", "code": GEN_MY_ACCOUNTS, "params_for_snapshot": {"viewer_email": "admin@example.com"},
            "parameters": [{"name": "viewer_email", "type": "string", "source": "identity", "identity_binding": "viewer.email"}]}
    report("params", "Filtered Sales", [regions, sales, mine],
           [{"title": "Filtered Sales", "mode": "page", "content": {"code": PARAMS_CODE}}], "public")

    report("edge", "Edge cases", [{"title": "KPIs", "code": GEN_KPIS}], [
        {"title": "Edge cases", "mode": "page", "content": {"code": EDGE_CODE}, "primary": True},
        {"title": "Notes", "mode": "doc", "content": {"markdown": DOC_MARKDOWN}},
    ], "internal")

    report("syntax", "Syntax error", [{"title": "KPIs", "code": GEN_KPIS}],
           [{"title": "Syntax error", "mode": "page", "content": {"code": SYNTAX_CODE}}], "public")
    report("runtime", "Runtime error", [{"title": "KPIs", "code": GEN_KPIS}],
           [{"title": "Runtime error", "mode": "page", "content": {"code": RUNTIME_CODE}}], "public")

    png = upload(client, headers, "gradient.png", _png_bytes(), "image/png")
    pdf = upload(client, headers, "doc.pdf", _pdf_bytes(), "application/pdf")
    files_code = FILES_CODE_TMPL.replace("__PNG__", png["id"]).replace("__PDF__", pdf["id"])
    report("files", "Embedded files", [], [{"title": "Embedded files", "mode": "page", "content": {
        "code": files_code, "visualization_ids": [],
        "files": [{"id": png["id"], "content_type": "image/png", "filename": "gradient.png"},
                  {"id": pdf["id"], "content_type": "application/pdf", "filename": "doc.pdf"}]}}], "public")

    report("slides", "HTML slides", [], [{"title": "HTML slides", "mode": "slides", "content": {"code": SLIDES_CODE}}], "public")
    report("legacy", "Legacy page", [{"title": "Monthly", "code": GEN_MONTHLY}],
           [{"title": "Legacy page", "mode": "page", "content": {"code": LEGACY_CODE}}], "public")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    sys.exit(main())
