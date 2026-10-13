"""Run Elasticsearch queries through the agent's real data path against a live
sandbox DB + cluster, and report which ones the connection's guardrails let
through.

construct_clients (same as create_data/inspect_data) -> wrap_clients_for_capture
(timeout/concurrency/rate-limit wrapper) -> ElasticsearchClient -> real cluster.

Usage (from backend/, same env as the running backend):
    python ../tools/agent/es_guardrails_probe.py cases            # guardrail matrix
    python ../tools/agent/es_guardrails_probe.py concurrency 6    # peak in-flight vs cap
    python ../tools/agent/es_guardrails_probe.py timeout          # slow query vs budget
Env: PROBE_DATA_SOURCE (default "Ops Logs"), PROBE_EMAIL (default admin@example.com),
     PROBE_INDEX (default IDX). See docs/feedback-loops/elasticsearch-query-guardrails.md.
"""
import asyncio, json, os, sys, time, threading
import main  # noqa: F401  registers mappers
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.user import User
from app.services.data_source_service import DataSourceService
from app.ai.code_execution.code_execution import wrap_clients_for_capture

IDX = os.environ.get("PROBE_INDEX", "applogs-*")

CASES = [
    ("named pattern search",       {"index": IDX, "size": 3, "_source": ["service", "level"]}),
    ("GET /_search equivalent '*'", {"index": "*", "size": 3}),
    ("'_all'",                      {"index": "_all", "size": 3}),
    ("finance via wildcard-only",   {"index": "*", "query": {"term": {"account": "ACME"}}}),
    ("finance by name",             {"index": "finance-secret", "size": 3}),
    ("ES|QL FROM *",                {"esql": "FROM * | STATS n = COUNT(*)"}),
    ("ES|QL // comment + FROM *",   {"esql": "// count\nFROM * | STATS n = COUNT(*)"}),
    ("URL-encoded '*' (%2A)",       {"index": "%2A", "query": {"term": {"account": "ACME"}}}),
    ("SQL FROM \"*\"",              {"sql": "SELECT COUNT(*) AS n FROM \"*\""}),
    ("SQL group by (ok)",           {"sql": f'SELECT service, COUNT(*) AS n FROM "{IDX}" GROUP BY service'}),
    ("SQL EXTRACT(.. FROM ..) (ok)", {"sql": f'SELECT EXTRACT(HOUR FROM "@timestamp") AS h, COUNT(*) AS n FROM "{IDX}" GROUP BY h'}),
    ("size 5000",                   {"index": IDX, "size": 5000}),
    ("ES|QL LIMIT 5000",            {"esql": f"FROM {IDX} | KEEP service | LIMIT 5000"}),
    ("1 terms agg (ok)",            {"index": IDX, "aggs": {"by_service": {"terms": {"field": "service"}}}}),
    ("4 aggs, depth 3",             {"index": IDX, "aggs": {"svc": {"terms": {"field": "service", "size": 6},
                                       "aggs": {"lvl": {"terms": {"field": "level", "size": 3},
                                         "aggs": {"p": {"avg": {"field": "latency_ms"}}}}}},
                                       "total": {"value_count": {"field": "status"}}}}),
    ("terms size 5000 x host 20",   {"index": IDX, "aggs": {"h": {"terms": {"field": "host", "size": 5000},
                                       "aggs": {"s": {"terms": {"field": "status", "size": 20}}}}}}),
    ("unparseable agg size",        {"index": IDX, "aggs": {"h": {"terms": {"field": "host", "size": "lots"}}}}),
    ("agent asks timeout 10m",      {"index": IDX, "size": 1, "timeout": "10m"}),
]

async def build(email):
    async with async_session_maker() as db:
        ds = (await db.execute(select(DataSource).where(DataSource.name == os.environ.get("PROBE_DATA_SOURCE", "Ops Logs")))).scalar_one()
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        org = await db.get(Organization, ds.organization_id)
        settings = await org.get_settings(db)
        clients = await DataSourceService().construct_clients(db, ds, current_user=user)
        return wrap_clients_for_capture(clients, [], [], usage_context=None, organization_settings=settings)

def main_():
    which = sys.argv[1] if len(sys.argv) > 1 else "cases"
    wrapped = asyncio.run(build(os.environ.get("PROBE_EMAIL", "admin@example.com")))
    key, client = next(iter(wrapped.items()))
    inner = client._original
    print(f"client={key} wrapper_timeout={client._query_timeout_seconds}s "
          f"engine_timeout={getattr(inner, 'query_timeout_seconds', 'n/a (base: fixed 60s)')} "
          f"max_concurrent={client._max_concurrent_queries}")
    if which == "cases":
        for name, q in CASES:
            sent = {}
            orig = inner._request
            def spy(method, path, json_body=None, params=None, timeout=None, _o=orig):
                sent.update(path=path, body=json_body, http_timeout=timeout)
                return _o(method, path, json_body=json_body, params=params, **({"timeout": timeout} if timeout else {}))
            try:
                inner._request = spy
                df = client.execute_query(json.dumps(q))
                extra = ""
                if "timeout" in (sent.get("body") or {}):
                    extra = f" engine_timeout={sent['body']['timeout']}"
                if sent.get("http_timeout"):
                    extra += f" http_read={sent['http_timeout'][1]}s"
                idx = sorted(set(df["_index"])) if "_index" in df.columns else ""
                print(f"  ALLOWED  {name:30s} rows={len(df):5d} {idx}{extra}")
            except Exception as e:
                print(f"  BLOCKED  {name:30s} {type(e).__name__}: {str(e)[:150]}"
                      + (" [reached cluster]" if sent else " [never reached cluster]"))
            finally:
                inner._request = orig
    elif which == "concurrency":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 6
        slow = {"index": IDX, "size": 0, "runtime_mappings": {"slow": {"type": "long",
                "script": {"source": "long s=0; long v=doc['status'].value; for (int i=0;i<400000;i++){s+=(v*i)%13;} emit(s);"}}},
                "aggs": {"x": {"sum": {"field": "slow"}}}}
        live = {"n": 0, "peak": 0}; lock = threading.Lock(); out = []
        orig = inner.execute_query
        def counting(q, _o=orig):
            with lock:
                live["n"] += 1; live["peak"] = max(live["peak"], live["n"])
            try:
                return _o(q)
            finally:
                with lock: live["n"] -= 1
        inner.execute_query = counting
        def run(i):
            t = time.monotonic()
            try:
                q = json.loads(json.dumps(slow)); q["runtime_mappings"]["slow"]["script"]["source"] = q["runtime_mappings"]["slow"]["script"]["source"].replace("%13", f"%{13+i}")
                client.execute_query(json.dumps(q)); out.append((i, "ok", time.monotonic() - t))
            except Exception as e:
                out.append((i, type(e).__name__, time.monotonic() - t))
        ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        t0 = time.monotonic(); [t.start() for t in ts]; [t.join() for t in ts]
        print(f"  {n} parallel queries: peak in-flight at the cluster = {live['peak']} "
              f"(cap {client._max_concurrent_queries}); wall {time.monotonic()-t0:.1f}s")
        for i, st, d in sorted(out): print(f"    q{i}: {st} after {d:.1f}s")
    elif which == "timeout":
        slow = {"index": IDX, "size": 0, "runtime_mappings": {"slow": {"type": "long",
                "script": {"source": "long s=0; long v=doc['status'].value; for (int i=0;i<900000;i++){s+=(v*i)%7;} emit(s);"}}},
                "aggs": {"x": {"sum": {"field": "slow"}}}}
        t = time.monotonic()
        try:
            df = client.execute_query(json.dumps(slow)); print(f"  slow query OK in {time.monotonic()-t:.1f}s rows={len(df)}")
        except Exception as e:
            print(f"  slow query {type(e).__name__} after {time.monotonic()-t:.1f}s: {str(e)[:220]}")

main_()
