#!/usr/bin/env python3
"""Seed a local Zabbix 7.0 with a realistic, sizable infrastructure dataset.

Config objects (host groups, hosts, trapper items, triggers, an API token) are
created via the JSON-RPC API. Bulk time-series (history/trends) and the
event/problem stream are COPY-loaded straight into Postgres for speed.

Usage:  python seed_zabbix.py   (Zabbix must be up on localhost:8080)
Prints the generated API token on the last line as: API_TOKEN=<token>
"""
import csv
import io
import json
import math
import os
import random
import subprocess
import time
import urllib.request

API = "http://127.0.0.1:8080/api_jsonrpc.php"
DB_CONTAINER = "zabbix-zabbix-db-1"
random.seed(1337)

NOW = int(time.time())
DAYS = 7
STEP = 300                      # 5-minute history resolution
START = NOW - DAYS * 86400
_rpcid = 0


def rpc(method, params, auth=None):
    global _rpcid
    _rpcid += 1
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": _rpcid}
    if auth:
        payload["auth"] = auth
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json-rpc"},
    )
    body = json.loads(urllib.request.urlopen(req, timeout=60).read())
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


def psql_copy(table, columns, rows):
    """COPY rows (list of tuples) into <table> via psql in the db container."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(r)
    cmd = [
        "docker", "exec", "-i", DB_CONTAINER,
        "psql", "-U", "zabbix", "-d", "zabbix", "-c",
        f"COPY {table} ({','.join(columns)}) FROM STDIN WITH (FORMAT csv)",
    ]
    p = subprocess.run(cmd, input=buf.getvalue(), text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"COPY {table} failed: {p.stderr[:500]}")


def maxid(table, col):
    cmd = ["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", "zabbix", "-d",
           "zabbix", "-tAc", f"SELECT COALESCE(MAX({col}),0) FROM {table}"]
    return int(subprocess.run(cmd, text=True, capture_output=True).stdout.strip() or "0")


# ── fleet definition ─────────────────────────────────────────────────────────
GROUPS = ["Linux servers", "Web servers", "Databases", "Cache", "Load balancers"]

# (prefix, count, group, role) — role picks the item template + baselines
FLEET = [
    ("web",   6, "Web servers",    "web"),
    ("app",   6, "Linux servers",  "app"),
    ("db",    4, "Databases",      "db"),
    ("cache", 2, "Cache",          "cache"),
    ("lb",    2, "Load balancers", "lb"),
]

# item template: key, display name, value_type (0 float / 3 uint), units, baseline, amplitude
COMMON_ITEMS = [
    ("system.cpu.util",       "CPU utilization",       0, "%",    35, 20),
    ("vm.memory.utilization", "Memory utilization",    0, "%",    55, 12),
    ("vfs.fs.size.pcent",     "Disk space used /",     0, "%",    60,  3),
    ("system.cpu.load",       "CPU load average (1m)", 0, "",      1.2, 0.8),
    ("net.if.in",             "Network in",            3, "bps", 4_000_000, 3_000_000),
    ("net.if.out",            "Network out",           3, "bps", 3_000_000, 2_500_000),
]
ROLE_ITEMS = {
    "web":   [("web.requests.rate", "HTTP requests/sec", 0, "rps", 180, 120),
              ("web.response.time", "Avg response time", 0, "s",   0.25, 0.2)],
    "app":   [("app.threads.active", "Active worker threads", 3, "", 40, 25)],
    "db":    [("db.connections.active", "Active DB connections", 3, "", 60, 40),
              ("db.queries.rate", "Queries/sec", 0, "qps", 900, 600),
              ("db.replication.lag", "Replication lag", 0, "s", 0.5, 1.5)],
    "cache": [("cache.hit.ratio", "Cache hit ratio", 0, "%", 94, 4),
              ("cache.evictions.rate", "Evictions/sec", 3, "", 5, 15)],
    "lb":    [("lb.active.connections", "Active connections", 3, "", 800, 500),
              ("lb.5xx.rate", "HTTP 5xx/sec", 0, "rps", 0.5, 3)],
}


def value_at(clock, baseline, amp, vtype, jitter=0.15, incident=None):
    """Diurnal sine + noise, clamped. `incident` = (start, end, mult) spike window."""
    tod = (clock % 86400) / 86400.0
    diurnal = math.sin((tod - 0.25) * 2 * math.pi)          # peak mid-day
    week = math.sin((clock - START) / (DAYS * 86400) * math.pi)  # gentle weekly hump
    v = baseline + amp * (0.6 * diurnal + 0.2 * week) + random.gauss(0, amp * jitter)
    if incident and incident[0] <= clock <= incident[1]:
        v *= incident[2]
    v = max(0.0, v)
    if "pcent" in "":  # placeholder
        pass
    if vtype == 3:
        return int(v)
    return round(v, 3)


def main():
    print("Logging in…")
    auth = rpc("user.login", {"username": "Admin", "password": "zabbix"})

    # groups
    print("Creating host groups…")
    gids = {}
    existing = {g["name"]: g["groupid"] for g in rpc("hostgroup.get", {"output": ["groupid", "name"]}, auth)}
    for name in GROUPS:
        if name in existing:
            gids[name] = existing[name]
        else:
            gids[name] = rpc("hostgroup.create", {"name": name}, auth)["groupids"][0]

    # hosts + items
    print("Creating hosts and items…")
    items = []   # (itemid, vtype, baseline, amp, key, hostname, role)
    host_ids = []
    for prefix, count, group, role in FLEET:
        template = COMMON_ITEMS + ROLE_ITEMS.get(role, [])
        for n in range(1, count + 1):
            hostname = f"{prefix}-{n:02d}"
            host = rpc("host.create", {
                "host": hostname,
                "name": f"{hostname}.acme.internal",
                "groups": [{"groupid": gids[group]}],
                "status": 0,
            }, auth)
            hostid = host["hostids"][0]
            host_ids.append((hostid, hostname, role))
            for key, iname, vtype, units, base, amp in template:
                it = rpc("item.create", {
                    "hostid": hostid, "name": iname, "key_": key,
                    "type": 2, "value_type": vtype, "units": units, "delay": "0",
                }, auth)
                items.append((it["itemids"][0], vtype, base, amp, key, hostname, role))

    print(f"  {len(host_ids)} hosts, {len(items)} items")

    # triggers on CPU + disk + response time (gives the `triggers` table content)
    print("Creating triggers…")
    trig_ids = []
    for hostid, hostname, role in host_ids:
        rpc("trigger.create", [{
            "description": f"High CPU utilization on {{HOST.NAME}}",
            "expression": f"last(/{hostname}/system.cpu.util)>90",
            "priority": 4,
        }], auth)
        rpc("trigger.create", [{
            "description": f"Disk space critically low on {{HOST.NAME}}",
            "expression": f"last(/{hostname}/vfs.fs.size.pcent)>90",
            "priority": 5,
        }], auth)
    trigs = rpc("trigger.get", {"output": ["triggerid", "description"],
                                "selectHosts": ["host"]}, auth)
    print(f"  {len(trigs)} triggers")

    # ── bulk history + trends ────────────────────────────────────────────────
    print("Generating history + trends (this is the sizable part)…")
    # Pick a couple of hosts to have a CPU incident window (day 5, 3-hour spike).
    incident_hosts = {"web-03", "db-02"}
    hist_f, hist_u, trend_f, trend_u = [], [], [], []
    for itemid, vtype, base, amp, key, hostname, role in items:
        incident = None
        if key == "system.cpu.util" and hostname in incident_hosts:
            istart = START + 5 * 86400 + 10 * 3600
            incident = (istart, istart + 3 * 3600, 2.4)
        if key == "web.response.time" and hostname in incident_hosts:
            istart = START + 5 * 86400 + 10 * 3600
            incident = (istart, istart + 3 * 3600, 5.0)
        # hourly buckets for trends
        hour_bucket = {}
        clock = START
        while clock <= NOW:
            v = value_at(clock, base, amp, vtype, incident=incident)
            if vtype == 3:
                hist_u.append((itemid, clock, int(v), 0))
            else:
                hist_f.append((itemid, clock, v, 0))
            hour_bucket.setdefault(clock - (clock % 3600), []).append(v)
            clock += STEP
        for hclock, vals in hour_bucket.items():
            row = (itemid, hclock, len(vals), min(vals), sum(vals) / len(vals), max(vals))
            if vtype == 3:
                trend_u.append((itemid, hclock, len(vals), int(min(vals)), int(sum(vals) / len(vals)), int(max(vals))))
            else:
                trend_f.append(row)

    print(f"  history float={len(hist_f)} uint={len(hist_u)}, trends float={len(trend_f)} uint={len(trend_u)}")
    psql_copy("history", ["itemid", "clock", "value", "ns"], hist_f)
    psql_copy("history_uint", ["itemid", "clock", "value", "ns"], hist_u)
    psql_copy("trends", ["itemid", "clock", "num", "value_min", "value_avg", "value_max"], trend_f)
    psql_copy("trends_uint", ["itemid", "clock", "num", "value_min", "value_avg", "value_max"], trend_u)

    # update items' lastvalue/lastclock so the `items` table shows current values
    subprocess.run(["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", "zabbix",
                    "-d", "zabbix", "-c",
                    "UPDATE items i SET lastvalue=sub.v::text, prevvalue=sub.v::text "
                    "FROM (SELECT itemid, value AS v FROM history h WHERE clock=(SELECT MAX(clock) FROM history) ) sub "
                    "WHERE i.itemid=sub.itemid"], capture_output=True, text=True)

    # ── events + problems ────────────────────────────────────────────────────
    print("Generating events + problems…")
    ev_base = maxid("events", "eventid") + 1
    events, problems = [], []
    eid = ev_base
    trig_by_host = {}
    for t in trigs:
        h = t["hosts"][0]["host"] if t.get("hosts") else "?"
        trig_by_host.setdefault(h, []).append(t)

    # ~60 problem events across the week; ~15 left unresolved (active)
    hosts_flat = [h for h, _, _ in host_ids]
    for i in range(60):
        host = random.choice(hosts_flat)
        cand = trig_by_host.get(host)
        if not cand:
            continue
        trig = random.choice(cand)
        sev = random.choice([2, 3, 3, 4, 4, 5])
        start = START + random.randint(0, DAYS * 86400 - 3600)
        name = trig["description"].replace("{HOST.NAME}", host)
        resolved = i >= 45  # last 15 stay open
        events.append((eid, 0, 0, int(trig["triggerid"]), start, 1,
                       random.randint(0, 1), 0, name, sev))
        r_eventid = ""
        r_clock = 0
        if not resolved:
            dur = random.randint(600, 6 * 3600)
            r_clock_val = min(start + dur, NOW)
            reid = eid + 100000
            events.append((reid, 0, 0, int(trig["triggerid"]), r_clock_val, 0, 1, 0,
                           name, sev))
            r_eventid = reid
            r_clock = r_clock_val
        problems.append((eid, 0, 0, int(trig["triggerid"]), start, 0,
                         r_eventid, r_clock, name, random.randint(0, 1), sev))
        eid += 1

    psql_copy("events", ["eventid", "source", "object", "objectid", "clock", "value",
                         "acknowledged", "ns", "name", "severity"], events)
    # problem table: only rows still open OR keep all (Zabbix keeps resolved in events,
    # active in problem). Insert unresolved-at-insert; resolved ones get r_eventid set.
    psql_copy("problem", ["eventid", "source", "object", "objectid", "clock", "ns",
                          "r_eventid", "r_clock", "name", "acknowledged", "severity"],
              [(p[0], p[1], p[2], p[3], p[4], p[5],
                p[6] if p[6] != "" else None, p[7], p[8], p[9], p[10]) for p in problems])
    active = sum(1 for p in problems if p[6] == "")
    print(f"  {len(problems)} problems ({active} active/open)")

    # ── API token for the connector ──────────────────────────────────────────
    print("Creating API token…")
    admin_uid = rpc("user.get", {"output": ["userid"], "filter": {"username": "Admin"}}, auth)[0]["userid"]
    tok = rpc("token.create", {"name": f"bow-connector-{NOW}", "userid": admin_uid,
                               "status": 0}, auth)
    tokenid = tok["tokenids"][0]
    token_str = rpc("token.generate", [tokenid], auth)[0]["token"]

    print("DONE")
    print(f"API_TOKEN={token_str}")


if __name__ == "__main__":
    main()
