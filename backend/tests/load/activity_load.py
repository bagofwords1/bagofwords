"""Scale test for report activity: 100 users, 100 SSE streams, 500 parallel
"runs" simulated by direct DB state transitions (the LLM is mocked at the
deepest level — no agent executes; the watcher/stream/snapshot only read DB
state, which is exactly what they do in production).

Phases:
  A) 500 reports go running at once (mass burst — worst case)
  B) all 500 finish
  C) snapshot endpoint hammered by all 100 users concurrently

Measures per-client event delivery latency (DB-write -> event received),
fan-out completeness, snapshot latency, and server RSS.
"""
import asyncio, json, statistics, sys, time, uuid, datetime, os
import asyncpg

import httpx

BASE = "http://localhost:8000"
DSN = 'postgresql://bow@127.0.0.1:5433/bow_load'
N_USERS = 100
N_REPORTS = 500

org_id = None
REPORT_IDS: list = []

results = {
    'phase_a': {},  # client_idx -> {report_id: latency_s}
    'phase_b': {},
    'errors': [],
}
write_ts = {'a': None, 'b': None}


def pct(vals, p):
    if not vals: return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * p))]


def server_rss_mb():
    try:
        pid = int(os.popen("pgrep -f 'uvicorn main:app' | head -1").read().strip())
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS'):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None


async def login_all():
    tokens = []
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        sem = asyncio.Semaphore(8)
        async def one(i):
            async with sem:
                r = await client.post(f"{BASE}/api/auth/jwt/login",
                                      data={"username": f"load_u{i}@test.com", "password": "test1234"})
                r.raise_for_status()
                return r.json()["access_token"]
        tokens = await asyncio.gather(*[one(i) for i in range(N_USERS)])
    return tokens


async def stream_client(idx, token, stop_evt):
    """Hold one SSE stream; record arrival time of each report's state events."""
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    seen_a = results['phase_a'].setdefault(idx, {})
    seen_b = results['phase_b'].setdefault(idx, {})
    try:
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            async with client.stream("GET", f"{BASE}/api/reports/activity/stream", headers=headers) as r:
                if r.status_code != 200:
                    results['errors'].append(f"client {idx}: HTTP {r.status_code}")
                    return
                buf = ""
                async for chunk in r.aiter_text():
                    if stop_evt.is_set():
                        return
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        data_line = next((l for l in frame.split("\n") if l.startswith("data: ")), None)
                        if not data_line:
                            continue
                        try:
                            ev = json.loads(data_line[6:])
                        except Exception:
                            continue
                        rid, state = ev.get('report_id'), ev.get('state')
                        t = time.monotonic()
                        if state == 'running' and write_ts['a'] and rid not in seen_a:
                            seen_a[rid] = t - write_ts['a']
                        elif state == 'idle' and write_ts['b'] and rid not in seen_b:
                            seen_b[rid] = t - write_ts['b']
    except Exception as e:
        if not stop_evt.is_set():
            results['errors'].append(f"client {idx}: {type(e).__name__} {e}")


async def db_start_runs():
    """500 parallel runs appear at once: one txn, like a mass schedule firing."""
    conn = await asyncpg.connect(DSN)
    now = datetime.datetime.utcnow()
    async with conn.transaction():
        await conn.executemany(
            "INSERT INTO completions (id, prompt, completion, status, model, turn_index, message_type, role, report_id, main_router, feedback_score, created_at, updated_at) "
            "VALUES ($1, $2, $3, 'in_progress', 'mock-llm', 0, 'ai_completion', 'system', $4, 'chat', 0, $5, $6)",
            [(str(uuid.uuid4()), json.dumps({"content": "load"}), json.dumps({}), rid, now, now) for rid in REPORT_IDS])
        await conn.execute("UPDATE reports SET last_activity_at=$1 WHERE id = ANY($2)", now, REPORT_IDS)
    await conn.close()
    return time.monotonic()


async def db_finish_runs():
    conn = await asyncpg.connect(DSN)
    now = datetime.datetime.utcnow()
    async with conn.transaction():
        await conn.execute("UPDATE completions SET status='success', updated_at=$1 WHERE model='mock-llm' AND status='in_progress'", now)
        await conn.execute("UPDATE reports SET last_activity_at=$1 WHERE id = ANY($2)", now, REPORT_IDS)
    await conn.close()
    return time.monotonic()


async def snapshot_hammer(tokens):
    """All 100 users snapshot 100 report ids concurrently."""
    lat = []
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        async def one(i):
            ids = ','.join(REPORT_IDS[i % 5 * 100:(i % 5) * 100 + 100])
            headers = {"Authorization": f"Bearer {tokens[i]}", "X-Organization-Id": org_id}
            t0 = time.monotonic()
            r = await client.get(f"{BASE}/api/reports/activity", params={"ids": ids}, headers=headers)
            lat.append(time.monotonic() - t0)
            if r.status_code != 200:
                results['errors'].append(f"snapshot {i}: HTTP {r.status_code}")
            return len(r.json().get('activity', []))
        counts = await asyncio.gather(*[one(i) for i in range(N_USERS)])
    return lat, counts


async def main():
    global org_id, REPORT_IDS
    org_id, report_line = open('scale_ids.txt').read().splitlines()
    REPORT_IDS = report_line.split(',')
    assert len(REPORT_IDS) == N_REPORTS
    print(f"RSS before: {server_rss_mb()} MB")
    t0 = time.time()
    tokens = await login_all()
    print(f"logged in {len(tokens)} users in {time.time()-t0:.1f}s")

    stop_evt = asyncio.Event()
    clients = [asyncio.create_task(stream_client(i, tokens[i], stop_evt)) for i in range(N_USERS)]
    await asyncio.sleep(5)  # all streams connected + first heartbeat window
    print(f"streams open; RSS: {server_rss_mb()} MB")

    # Phase A: 500 runs appear at once
    write_ts['a'] = await db_start_runs()
    print("phase A written (500 in_progress)")
    await asyncio.sleep(20)

    # Phase C (while events settle): snapshot hammer
    lat, counts = await snapshot_hammer(tokens)
    print(f"snapshot hammer done; visible counts sample: {counts[:3]}")

    # Phase B: all 500 finish
    write_ts['b'] = await db_finish_runs()
    print("phase B written (500 -> success)")
    await asyncio.sleep(20)

    print(f"RSS under load: {server_rss_mb()} MB")
    stop_evt.set()
    await asyncio.sleep(1)
    for c in clients: c.cancel()

    # ── report ──
    def phase_stats(name):
        per_client_complete = [len(v) for v in results[name].values()]
        all_lat = [l for v in results[name].values() for l in v.values()]
        print(f"\n{name}: clients={len(results[name])} "
              f"complete(min/median/max)={min(per_client_complete)}/{int(statistics.median(per_client_complete))}/{max(per_client_complete)} of {N_REPORTS}")
        if all_lat:
            print(f"  delivery latency s: p50={pct(all_lat,0.5):.2f} p95={pct(all_lat,0.95):.2f} p99={pct(all_lat,0.99):.2f} max={max(all_lat):.2f} (n={len(all_lat)})")
    phase_stats('phase_a')
    phase_stats('phase_b')
    print(f"\nsnapshot GET /reports/activity (100 ids, 100 concurrent): "
          f"p50={pct(lat,0.5)*1000:.0f}ms p95={pct(lat,0.95)*1000:.0f}ms max={max(lat)*1000:.0f}ms")
    print(f"errors: {len(results['errors'])}")
    for e in results['errors'][:10]: print("  ", e)

if __name__ == '__main__':
    asyncio.run(main())
