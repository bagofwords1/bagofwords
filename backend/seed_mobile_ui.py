"""Seed a public report + page artifact for mobile-UI screenshots (raw SQL).

Uses sqlite3 directly to avoid configuring unused/broken ORM mappers.
Run from backend/:  python3 seed_mobile_ui.py
"""
import json
import sqlite3
import uuid
from datetime import datetime

DB = "db/app.db"

ARTIFACT_CODE = r"""<script type="text/babel">
function Bar({ label, value, max }) {
  const pct = max ? Math.round((value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-3 mb-2">
      <div className="w-28 shrink-0 text-sm text-gray-600 truncate">{label}</div>
      <div className="flex-1 bg-gray-100 rounded h-6 overflow-hidden">
        <div className="h-6 bg-blue-600 rounded" style={{ width: pct + '%' }} />
      </div>
      <div className="w-14 text-right text-sm font-medium text-gray-800">{value}</div>
    </div>
  );
}
function KPI({ label, value, sub }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className="text-2xl font-semibold text-gray-900">{value}</div>
      {sub ? <div className="text-xs text-green-600 mt-1">{sub}</div> : null}
    </div>
  );
}
function App() {
  const rows = [
    { g: 'Rock', v: 820 }, { g: 'Metal', v: 380 }, { g: 'TV Shows', v: 255 },
    { g: 'Blues', v: 240 }, { g: 'R&B/Soul', v: 90 }, { g: 'Sci Fi & Fantasy', v: 74 },
    { g: 'Pop', v: 60 }, { g: 'Comedy', v: 41 }, { g: 'Bossa Nova', v: 30 },
  ];
  const max = Math.max.apply(null, rows.map(r => r.v));
  const total = rows.reduce((a, r) => a + r.v, 0);
  return (
    <div className="min-h-screen bg-white p-6">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-2xl font-bold text-gray-900 mb-1">Sales by Category</h1>
        <p className="text-sm text-gray-500 mb-6">Music store — top genres by units sold</p>
        <div className="grid grid-cols-3 gap-3 mb-6">
          <KPI label="Total Units" value={total.toLocaleString()} sub="+12% MoM" />
          <KPI label="Top Genre" value="Rock" sub="820 units" />
          <KPI label="Categories" value={rows.length} />
        </div>
        <div className="rounded-xl border border-gray-200 bg-white p-5">
          <div className="text-sm font-medium text-gray-700 mb-4">Units by Genre</div>
          {rows.map((r, i) => <Bar key={i} label={r.g} value={r.v} max={max} />)}
        </div>
      </div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

TITLE = "מכירות לפי קטגוריה (ז'אנר)"  # Hebrew: "Sales by category (genre)"


def main():
    c = sqlite3.connect(DB)
    org = c.execute("SELECT id FROM organizations LIMIT 1").fetchone()[0]
    user = c.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    now = datetime.utcnow().isoformat(sep=" ")

    report_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    slug = "mobile-ui-" + uuid.uuid4().hex[:8]

    c.execute(
        """INSERT INTO reports
           (id, title, slug, status, report_type, mode, artifact_visibility,
            conversation_visibility, user_id, organization_id, created_at,
            updated_at, last_activity_at, last_run_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (report_id, TITLE, slug, "published", "regular", "chat", "public",
         "public", user, org, now, now, now, now),
    )
    c.execute(
        """INSERT INTO artifacts
           (id, report_id, user_id, organization_id, title, mode, version,
            content, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (artifact_id, report_id, user, org, TITLE, "page", 1,
         json.dumps({"code": ARTIFACT_CODE}), "completed", now, now),
    )
    c.commit()
    print("REPORT_ID", report_id)
    print("ARTIFACT_ID", artifact_id)
    print("PUBLIC_URL /r/%s" % report_id)
    print("APP_URL /reports/%s" % report_id)


if __name__ == "__main__":
    main()
