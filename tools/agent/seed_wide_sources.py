#!/usr/bin/env python3
"""Build very wide synthetic data sources for the column usage, column cap
and column search work. Every name here is generic and made up.

Three local sources:

* **PostgreSQL** database ``wide_synth`` (~2,000 tables):
    - ``app_accounts`` / ``app_events`` / ``app_sessions`` / ``app_orders``:
      small, related tables (PK/FK) the agent can join;
    - ``metrics_wide_001`` .. ``metrics_wide_006``: 1,600 columns each, the
      PostgreSQL maximum. Families of columns share a prefix (``cpu_``,
      ``mem_``, ``disk_``, ``net_``, ``svc_``, ``app_``, ``kpi_``), and a few
      meaningful columns sit deep in the list (``net_retransmit_rate``,
      ``svc_error_budget_pct``, ``region_code``) so a column cap folds them
      out at cold start;
    - ``tbl_0001`` .. ``tbl_1990``: narrow filler tables (6-40 columns).

  PostgreSQL cannot hold a 5,000-column relation: tables AND views stop at
  1,600 columns, and a SELECT list stops at 1,664 entries (``target lists can
  have at most 1664 entries``). The 5,000-column SQL tables therefore live in
  DuckDB, below.

* **DuckDB** file ``wide_synth.duckdb``: ``events_wide_5000`` and
  ``telemetry_wide_5000`` (5,000 columns each, real SQL engine, no column
  limit), joined to a small ``dim_region`` table.

* **Elasticsearch** (single node, security off, localhost):
    - ``svc-events-wide``: ~5,000 mapped fields as BOW flattens them (nested
      objects ``svc.request.*``, ``process.*``, ``host.*``, ``user.*``,
      ``net.*``, ``geo.*``, a ``nested`` ``items`` array, and ``text`` fields
      with ``.keyword`` multi-fields);
    - ``obs-logs-2026.11.20`` / ``obs-logs-2026.11.21``: two daily indices with
      different field sets that BOW collapses into one ``obs-logs-*`` pattern
      table (merged mappings, the way index patterns grow).

Usage (from backend/, so the venv has duckdb + httpx):

    uv run python ../tools/agent/seed_wide_sources.py all
    uv run python ../tools/agent/seed_wide_sources.py postgres|duckdb|elastic

Env: PG_SUPERUSER_CMD (default "sudo -u postgres psql"), ES_URL
(default http://127.0.0.1:9200), DUCKDB_PATH (default
/tmp/bow-wide/wide_synth.duckdb).
"""
from __future__ import annotations

import json
import os
import random
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

RNG = random.Random(215)

PG_DB = "wide_synth"
PG_USER = "synth_reader"
PG_PASSWORD = "synth_reader_pw"  # local throwaway fixture credential
PSQL = shlex.split(os.environ.get("PG_SUPERUSER_CMD", "sudo -u postgres psql"))
ES_URL = os.environ.get("ES_URL", "http://127.0.0.1:9200")
DUCKDB_PATH = Path(os.environ.get("DUCKDB_PATH", "/tmp/bow-wide/wide_synth.duckdb"))

# Column families for wide tables: prefix -> metric stems.
FAMILIES = {
    "cpu": ["user_pct", "system_pct", "iowait_pct", "steal_pct", "idle_pct", "load_1m", "load_5m", "load_15m"],
    "mem": ["used_bytes", "free_bytes", "cached_bytes", "swap_used_bytes", "rss_bytes", "heap_bytes"],
    "disk": ["read_bytes", "write_bytes", "read_ops", "write_ops", "queue_depth", "util_pct"],
    "net": ["rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "drops"],
    "svc": ["request_count", "error_count", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "saturation"],
    "app": ["queue_len", "jobs_done", "jobs_failed", "cache_hits", "cache_misses", "gc_pause_ms"],
    "kpi": ["signups", "logins", "checkouts", "refunds", "active_users", "conversion_pct"],
}
# Meaningful columns placed deep in the wide tables (position matters: a
# column cap at cold start keeps the first/structural ones and folds these).
DEEP_COLUMNS = {
    "net_retransmit_rate": "double precision",
    "svc_error_budget_pct": "double precision",
    "region_code": "text",
    "tenant_tier": "text",
}


def wide_column_names(total: int, deep: dict[str, str], metric_type: str = "double precision") -> list[tuple[str, str]]:
    """`total` columns: a few structural ones first, then prefix families in
    round-robin shards, with `deep` columns inserted near the end."""
    cols: list[tuple[str, str]] = [
        ("row_id", "bigint"), ("event_time", "timestamp"), ("account_id", "bigint"), ("host_name", "text"),
    ]
    budget = total - len(cols) - len(deep)
    names: list[tuple[str, str]] = []
    shard = 0
    while len(names) < budget:
        for fam, stems in FAMILIES.items():
            for stem in stems:
                if len(names) >= budget:
                    break
                names.append((f"{fam}_{stem}_{shard:03d}", metric_type))
        shard += 1
    insert_at = int(len(names) * 0.85)
    deep_items = list(deep.items())
    return cols + names[:insert_at] + deep_items + names[insert_at:]


# ---------------------------------------------------------------- postgres
def psql(sql: str, db: str | None = None) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
        f.write(sql)
        path = f.name
    os.chmod(path, 0o644)
    cmd = PSQL + ["-v", "ON_ERROR_STOP=1", "-q"] + (["-d", db] if db else []) + ["-f", path]
    try:
        subprocess.run(cmd, check=True)
    finally:
        os.unlink(path)


def seed_postgres() -> None:
    psql(f"""
DROP DATABASE IF EXISTS {PG_DB};
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PG_USER}') THEN
    CREATE ROLE {PG_USER} LOGIN PASSWORD '{PG_PASSWORD}';
  END IF;
END $$;
CREATE DATABASE {PG_DB} OWNER {PG_USER};
""")
    parts: list[str] = ["BEGIN;"]
    parts.append("""
CREATE TABLE app_accounts (account_id bigint PRIMARY KEY, account_name text, plan_tier text,
  region_code text, created_at timestamp);
CREATE TABLE app_sessions (session_id bigint PRIMARY KEY, account_id bigint REFERENCES app_accounts(account_id),
  started_at timestamp, ended_at timestamp, device_kind text);
CREATE TABLE app_events (event_id bigint PRIMARY KEY, session_id bigint REFERENCES app_sessions(session_id),
  account_id bigint REFERENCES app_accounts(account_id), event_time timestamp, event_kind text, value_num double precision);
CREATE TABLE app_orders (order_id bigint PRIMARY KEY, account_id bigint REFERENCES app_accounts(account_id),
  ordered_at timestamp, amount double precision, status text);
INSERT INTO app_accounts SELECT g, 'account_' || g, (ARRAY['free','team','scale'])[1 + g % 3],
  (ARRAY['north','south','east','west'])[1 + g % 4], now() - (g || ' days')::interval FROM generate_series(1, 200) g;
INSERT INTO app_sessions SELECT g, 1 + g % 200, now() - (g || ' hours')::interval, now() - (g || ' hours')::interval + interval '20 min',
  (ARRAY['web','ios','android'])[1 + g % 3] FROM generate_series(1, 2000) g;
INSERT INTO app_events SELECT g, 1 + g % 2000, 1 + g % 200, now() - (g || ' minutes')::interval,
  (ARRAY['view','click','search','purchase'])[1 + g % 4], (g % 97)::double precision FROM generate_series(1, 20000) g;
INSERT INTO app_orders SELECT g, 1 + g % 200, now() - (g || ' hours')::interval, (g % 500) + 0.99,
  (ARRAY['paid','refunded','pending'])[1 + g % 3] FROM generate_series(1, 3000) g;
""")
    # Six 1,600-column tables (the PostgreSQL maximum).
    for i in range(1, 7):
        # `real` (4 bytes): 1,600 doubles would overflow PostgreSQL's 8 KB row.
        cols = wide_column_names(1600, {**DEEP_COLUMNS, "net_retransmit_rate": "real", "svc_error_budget_pct": "real"}, "real")
        name = f"metrics_wide_{i:03d}"
        ddl = ",\n  ".join(f"{c} {t}" + (" PRIMARY KEY" if c == "row_id" else "") for c, t in cols)
        parts.append(f"CREATE TABLE {name} (\n  {ddl}\n);")
        exprs = []
        for c, t in cols:
            if c == "row_id":
                exprs.append("g")
            elif c == "event_time":
                exprs.append("now() - (g || ' minutes')::interval")
            elif c == "account_id":
                exprs.append("1 + g % 200")
            elif c == "host_name":
                exprs.append("'host-' || (g % 12)")
            elif c == "region_code":
                exprs.append("(ARRAY['north','south','east','west'])[1 + g % 4]")
            elif c == "tenant_tier":
                exprs.append("(ARRAY['bronze','silver','gold'])[1 + g % 3]")
            else:
                exprs.append(f"round((random() * 100)::numeric, 3)::{t}")
        parts.append(
            f"INSERT INTO {name} ({', '.join(c for c, _ in cols)}) SELECT {', '.join(exprs)} "
            f"FROM generate_series(1, 300) g;"
        )
    # 1,990 narrow filler tables.
    stems = ["id", "created_at", "updated_at", "status", "owner_id", "label", "amount", "quantity",
             "score", "category", "source", "ref_code", "is_enabled", "note", "priority", "batch_id"]
    for i in range(1, 1991):
        n = 6 + (i * 7) % 35
        cols = []
        for j in range(n):
            stem = stems[j % len(stems)]
            cname = stem if j < len(stems) else f"{stem}_{j}"
            ctype = "bigint" if stem.endswith("id") else "timestamp" if stem.endswith("_at") else "text"
            cols.append(f"{cname} {ctype}" + (" PRIMARY KEY" if cname == "id" else ""))
        parts.append(f"CREATE TABLE tbl_{i:04d} ({', '.join(cols)});")
        if i % 50 == 0:
            parts.append(f"INSERT INTO tbl_{i:04d} (id, status) SELECT g, 'ok' FROM generate_series(1, 20) g;")
    parts.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {PG_USER};")
    parts.append("COMMIT;")
    psql("\n".join(parts), db=PG_DB)
    print(f"postgres: {PG_DB} ready (user {PG_USER})")


# ---------------------------------------------------------------- duckdb
def seed_duckdb() -> None:
    import duckdb

    DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DUCKDB_PATH.exists():
        DUCKDB_PATH.unlink()
    con = duckdb.connect(str(DUCKDB_PATH))
    con.execute("CREATE TABLE dim_region (region_code VARCHAR PRIMARY KEY, region_name VARCHAR, sla_target_pct DOUBLE)")
    con.execute("INSERT INTO dim_region VALUES ('north','North',99.9),('south','South',99.5),('east','East',99.9),('west','West',99.0)")
    for name in ("events_wide_5000", "telemetry_wide_5000"):
        cols = wide_column_names(5000, {**DEEP_COLUMNS, "checkout_abandon_rate": "double precision"})
        ddl_type = {"bigint": "BIGINT", "timestamp": "TIMESTAMP", "text": "VARCHAR", "double precision": "DOUBLE"}
        con.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {ddl_type[t]}' for c, t in cols)})")
        exprs = []
        for c, t in cols:
            if c == "row_id":
                exprs.append("g")
            elif c == "event_time":
                exprs.append("now() - to_minutes(g::BIGINT)")
            elif c == "account_id":
                exprs.append("1 + g % 200")
            elif c == "host_name":
                exprs.append("'host-' || (g % 12)")
            elif c == "region_code":
                exprs.append("['north','south','east','west'][1 + (g % 4)::INT]")
            elif c == "tenant_tier":
                exprs.append("['bronze','silver','gold'][1 + (g % 3)::INT]")
            else:
                exprs.append("round(random() * 100, 3)")
        con.execute(f"INSERT INTO {name} SELECT {', '.join(exprs)} FROM range(1, 201) t(g)")
    con.close()
    os.chmod(DUCKDB_PATH, 0o644)
    print(f"duckdb: {DUCKDB_PATH} ready")


# ---------------------------------------------------------------- elastic
TEXT_FIELD = {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}


def _es_props(prefix_tree: dict) -> dict:
    return prefix_tree


def es_wide_mapping(target_columns: int) -> tuple[dict, list[str]]:
    """Build a mapping whose BOW-flattened column count is ~target_columns.
    Returns (mapping properties, list of leaf field paths)."""
    groups = {
        "svc": ["request", "response", "upstream", "route", "auth"],
        "process": ["cpu", "memory", "io", "thread", "parent"],
        "host": ["os", "disk", "net", "boot", "container"],
        "user": ["profile", "session", "device", "consent"],
        "net": ["tcp", "udp", "tls", "dns", "http"],
        "geo": ["client", "server", "edge"],
    }
    leaf_kinds = ["long", "double", "keyword", "text", "date", "boolean", "ip", "float"]
    props: dict = {
        "@timestamp": {"type": "date"},
        "event": {"properties": {"id": {"type": "keyword"}, "kind": {"type": "keyword"},
                                 "outcome": {"type": "keyword"}, "duration_ms": {"type": "long"}}},
        "message": TEXT_FIELD,
        "items": {"type": "nested", "properties": {"sku": {"type": "keyword"}, "qty": {"type": "integer"},
                                                    "price": {"type": "double"}}},
    }
    leaves = ["@timestamp", "event.id", "event.kind", "event.outcome", "event.duration_ms", "message"]
    count = 1 + 4 + 2 + 4  # BOW columns so far (message + message.keyword, items + 3 children)
    # Deep, meaningful fields the agent should find by column search.
    props["svc"] = {"properties": {"request": {"properties": {
        "retry_budget_remaining": {"type": "long"},
        "tenant_tier": TEXT_FIELD,
    }}}}
    leaves += ["svc.request.retry_budget_remaining", "svc.request.tenant_tier"]
    count += 3
    i = 0
    while count < target_columns:
        for top, subs in groups.items():
            for sub in subs:
                if count >= target_columns:
                    break
                kind = leaf_kinds[i % len(leaf_kinds)]
                field = f"attr_{i:04d}"
                node = props.setdefault(top, {"properties": {}})["properties"].setdefault(sub, {"properties": {}})["properties"]
                node[field] = TEXT_FIELD if kind == "text" else {"type": kind}
                leaves.append(f"{top}.{sub}.{field}")
                count += 2 if kind == "text" else 1
                i += 1
    return props, leaves


def _leaf_value(path: str, kind: str, n: int):
    if kind in ("long", "integer"):
        return n % 1000
    if kind in ("double", "float"):
        return round(RNG.random() * 100, 3)
    if kind == "boolean":
        return n % 2 == 0
    if kind == "date":
        return f"2026-11-{1 + n % 20:02d}T{n % 24:02d}:00:00Z"
    if kind == "ip":
        return f"10.0.{n % 255}.{(n * 7) % 255}"
    return f"v{n % 17}"


def _set_path(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _kind_at(props: dict, path: str) -> str:
    cur = {"properties": props}
    for p in path.split("."):
        cur = (cur.get("properties") or {}).get(p) or {}
    return cur.get("type", "keyword")


def seed_elastic() -> None:
    import httpx

    c = httpx.Client(base_url=ES_URL, timeout=120)
    for name in ("svc-events-wide", "obs-logs-2026.11.20", "obs-logs-2026.11.21"):
        c.delete(f"/{name}")
    settings = {"index": {"mapping": {"total_fields": {"limit": 20000}}, "number_of_replicas": 0}}

    props, leaves = es_wide_mapping(5000)
    r = c.put("/svc-events-wide", json={"settings": settings, "mappings": {"properties": props}})
    r.raise_for_status()
    bulk = []
    tiers = ["bronze", "silver", "gold"]
    outcomes = ["success", "failure", "unknown"]
    for n in range(1, 501):
        doc: dict = {"@timestamp": f"2026-11-{1 + n % 20:02d}T{n % 24:02d}:{n % 60:02d}:00Z",
                     "event": {"id": f"e{n}", "kind": ["request", "job", "alert"][n % 3],
                               "outcome": outcomes[n % 3], "duration_ms": (n * 37) % 900},
                     "message": f"synthetic event {n}",
                     "items": [{"sku": f"sku-{n % 9}", "qty": 1 + n % 4, "price": round(5 + n % 50, 2)}]}
        _set_path(doc, "svc.request.retry_budget_remaining", (n * 13) % 100)
        _set_path(doc, "svc.request.tenant_tier", tiers[n % 3])
        for path in RNG.sample(leaves[8:], 60):
            _set_path(doc, path, _leaf_value(path, _kind_at(props, path), n))
        bulk.append(json.dumps({"index": {"_index": "svc-events-wide"}}))
        bulk.append(json.dumps(doc))
    r = c.post("/_bulk?refresh=true", content="\n".join(bulk) + "\n", headers={"Content-Type": "application/x-ndjson"})
    r.raise_for_status()
    assert not r.json().get("errors"), r.text[:500]

    # Two daily indices with overlapping-but-different mappings -> one pattern.
    for day, offset in (("2026.11.20", 0), ("2026.11.21", 700)):
        p: dict = {"@timestamp": {"type": "date"}, "log": {"properties": {"level": {"type": "keyword"}}},
                   "message": TEXT_FIELD, "trace": {"properties": {"id": {"type": "keyword"}}}}
        for k in range(offset, offset + 1500):
            p.setdefault("labels", {"properties": {}})["properties"][f"label_{k:04d}"] = {"type": "keyword"}
        name = f"obs-logs-{day}"
        c.put(f"/{name}", json={"settings": settings, "mappings": {"properties": p}}).raise_for_status()
        docs = []
        for n in range(1, 201):
            d = {"@timestamp": f"2026-11-{day[-2:]}T{n % 24:02d}:00:00Z", "log": {"level": ["info", "warn", "error"][n % 3]},
                 "message": f"log line {n}", "trace": {"id": f"t{n}"},
                 "labels": {f"label_{offset + (n % 1500):04d}": f"x{n % 5}"}}
            docs.append(json.dumps({"index": {"_index": name}}))
            docs.append(json.dumps(d))
        c.post("/_bulk?refresh=true", content="\n".join(docs) + "\n",
               headers={"Content-Type": "application/x-ndjson"}).raise_for_status()
    fields = c.get("/svc-events-wide/_mapping/field/*").json()
    print(f"elastic: svc-events-wide mapped leaf+multi fields = {len(fields.get('svc-events-wide', {}).get('mappings', {}))}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "postgres"):
        seed_postgres()
    if what in ("all", "duckdb"):
        seed_duckdb()
    if what in ("all", "elastic"):
        seed_elastic()
