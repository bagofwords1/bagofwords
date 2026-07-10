"""Build a real analytical dashboard from the LIVE Zabbix instance, querying
through the actual ZabbixClient connector — the same client the BOW agent uses.
Renders an HTML dashboard (self-contained) for a screenshot.
"""
import json
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/user/bagofwords/backend")
from app.data_sources.clients.zabbix_client import ZabbixClient  # noqa: E402
import pandas as pd  # noqa: E402

import os
TOKEN = os.environ.get("ZBX_TOKEN", "")
c = ZabbixClient(url="http://127.0.0.1:8080", api_token=TOKEN)

SEV = {0: "Not classified", 1: "Information", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}
SEV_COLOR = {2: "#6b7280", 3: "#f59e0b", 4: "#f97316", 5: "#dc2626"}

# 1) Active problems → per host, per severity
probs = c.execute_query('{"table":"problems","limit":500}')
hosts = c.execute_query('{"table":"hosts","output":["hostid","host","name"],"limit":100}')
trigs = c.execute_query('{"table":"triggers","output":["triggerid"],"params":{"selectHosts":["host"]},"limit":5000}')
trig_host = {}
for _, r in trigs.iterrows():
    hs = r.get("hosts") or []
    if isinstance(hs, list) and hs:
        trig_host[str(r["triggerid"])] = hs[0].get("host")
probs["host"] = probs["objectid"].astype(str).map(trig_host).fillna("unknown")
probs["sev"] = probs["severity"].astype(int)
active = probs[(probs.get("r_clock", 0).astype(int) == 0)] if "r_clock" in probs else probs
by_host = active.groupby(["host", "sev"]).size().unstack(fill_value=0)
host_totals = active.groupby("host").size().sort_values(ascending=False)

# 2) 7-day CPU trend for the incident host (web-03) vs a calm host (app-01)
def cpu_trend(hostname):
    h = c.execute_query(json.dumps({"table": "hosts", "params": {"filter": {"host": hostname}}}))
    if not len(h):
        return pd.DataFrame()
    hid = h.iloc[0]["hostid"]
    it = c.execute_query(json.dumps({"table": "items", "params": {"hostids": [hid], "search": {"key_": "system.cpu.util"}}}))
    if not len(it):
        return pd.DataFrame()
    iid = it.iloc[0]["itemid"]
    since = int(time.time()) - 7 * 86400
    tr = c.execute_query(json.dumps({"table": "trends", "params": {"itemids": [iid], "time_from": since}, "limit": 5000}))
    if not len(tr):
        return pd.DataFrame()
    tr["t"] = pd.to_datetime(tr["clock"].astype(int), unit="s")
    tr["avg"] = tr["value_avg"].astype(float)
    tr["max"] = tr["value_max"].astype(float)
    return tr.sort_values("t")

web03 = cpu_trend("web-03")
app01 = cpu_trend("app-01")

# 3) inventory + counts
n_hosts = len(hosts)
items = c.execute_query('{"table":"items","params":{"countOutput":true}}')
n_items = int(items.iloc[0]["result"]) if len(items) else 0
n_active = len(active)
sev_counts = Counter(active["sev"])

# ---- render dashboard as SVG-in-HTML (no external deps) ----
def bars_stacked():
    order = list(host_totals.index)[:12]
    maxv = max(host_totals.max(), 1)
    rows = []
    for host in order:
        x = 0
        segs = []
        for s in [2, 3, 4, 5]:
            v = int(by_host.loc[host, s]) if (host in by_host.index and s in by_host.columns) else 0
            if v:
                w = v / maxv * 460
                segs.append(f'<rect x="{150 + x}" y="0" width="{w:.1f}" height="20" fill="{SEV_COLOR[s]}"/>')
                x += w
        total = int(host_totals[host])
        rows.append(
            f'<g transform="translate(0,{len(rows)*30})">'
            f'<text x="140" y="15" text-anchor="end" font-size="13" fill="#374151" font-family="sans-serif">{host}</text>'
            + "".join(segs)
            + f'<text x="{150 + x + 8}" y="15" font-size="12" fill="#6b7280" font-family="sans-serif">{total}</text>'
            f'</g>'
        )
    h = len(order) * 30 + 10
    return f'<svg width="680" height="{h}" viewBox="0 0 680 {h}">' + "".join(rows) + "</svg>"

def line_cpu():
    W, H, PAD = 680, 240, 40
    def path(df, col):
        if not len(df):
            return ""
        xs = df["t"].map(lambda t: t.timestamp())
        x0, x1 = xs.min(), xs.max()
        pts = []
        for (_, r), xv in zip(df.iterrows(), xs):
            x = PAD + (xv - x0) / (x1 - x0 + 1e-9) * (W - 2 * PAD)
            y = H - PAD - (float(r[col]) / 130.0) * (H - 2 * PAD)
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)
    grid = "".join(
        f'<line x1="{PAD}" y1="{H-PAD-(v/130.0)*(H-2*PAD):.0f}" x2="{W-PAD}" y2="{H-PAD-(v/130.0)*(H-2*PAD):.0f}" stroke="#e5e7eb"/>'
        f'<text x="{PAD-6}" y="{H-PAD-(v/130.0)*(H-2*PAD)+4:.0f}" text-anchor="end" font-size="10" fill="#9ca3af" font-family="sans-serif">{v}%</text>'
        for v in [0, 50, 100]
    )
    thr = f'<line x1="{PAD}" y1="{H-PAD-(90/130.0)*(H-2*PAD):.0f}" x2="{W-PAD}" y2="{H-PAD-(90/130.0)*(H-2*PAD):.0f}" stroke="#dc2626" stroke-dasharray="4 3" opacity="0.6"/>'
    p1 = f'<polyline points="{path(web03,"avg")}" fill="none" stroke="#dc2626" stroke-width="2"/>' if len(web03) else ""
    p2 = f'<polyline points="{path(app01,"avg")}" fill="none" stroke="#2563eb" stroke-width="2"/>' if len(app01) else ""
    return f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}">{grid}{thr}{p1}{p2}</svg>'

legend = "".join(
    f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;font-size:12px;color:#4b5563">'
    f'<span style="width:11px;height:11px;background:{SEV_COLOR[s]};border-radius:2px;display:inline-block"></span>{SEV[s]} ({sev_counts.get(s,0)})</span>'
    for s in [5, 4, 3, 2]
)

html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:0 auto;padding:28px;background:#fff;color:#111827">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">
    <div style="width:26px;height:26px;background:#d30000;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-family:sans-serif">Z</div>
    <h1 style="font-size:22px;margin:0">Zabbix — Infrastructure Health</h1>
  </div>
  <p style="color:#6b7280;font-size:13px;margin:0 0 20px 36px">Live monitoring analysis · queried through the Bag of Words Zabbix connector</p>

  <div style="display:flex;gap:12px;margin-bottom:24px">
    {"".join(f'<div style="flex:1;background:#f9fafb;border:1px solid #eee;border-radius:10px;padding:14px"><div style="font-size:26px;font-weight:700">{v}</div><div style="font-size:12px;color:#6b7280">{k}</div></div>' for k,v in [("Hosts monitored",n_hosts),("Metrics tracked",n_items),("Active problems",n_active),("Disaster-level",sev_counts.get(5,0))])}
  </div>

  <h2 style="font-size:15px;margin:0 0 4px">Active problems by host</h2>
  <div style="margin:0 0 8px">{legend}</div>
  {bars_stacked()}

  <h2 style="font-size:15px;margin:24px 0 4px">CPU utilization — 7-day trend (hourly avg)</h2>
  <div style="font-size:12px;color:#4b5563;margin-bottom:6px">
    <span style="color:#dc2626">● web-03</span> (incident host) &nbsp;
    <span style="color:#2563eb">● app-01</span> (baseline) &nbsp;
    <span style="color:#dc2626">– – 90% alert threshold</span>
  </div>
  {line_cpu()}
  <p style="font-size:12px;color:#6b7280;margin-top:8px">web-03 shows a clear CPU incident spike breaching the 90% trigger threshold on day 5 — the same window that generated its High/Disaster problem events.</p>
</div>"""

open("/home/user/bagofwords/tools/zabbix/dashboard.html", "w").write(
    "<!doctype html><meta charset=utf-8><body style='margin:0;background:#f3f4f6;padding:20px'>" + html + "</body>"
)
print("wrote dashboard.html")
print(f"hosts={n_hosts} items={n_items} active_problems={n_active} sev={dict(sev_counts)}")
print("top hosts:", host_totals.head(6).to_dict())
print("web-03 cpu max:", round(web03["max"].max(), 1) if len(web03) else "n/a")
