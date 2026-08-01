import asyncio, json, time, datetime, uuid
import asyncpg, httpx

BASE = "http://localhost:8000"
DSN = 'postgresql://bow@127.0.0.1:5433/bow_load'
def _load_ids():
    org_id, report_line = open('scale_ids.txt').read().splitlines()
    return org_id, report_line.split(',')[0]

def pct(vals, p):
    vals = sorted(vals); return vals[min(len(vals)-1, int(len(vals)*p))]

async def main():
    org_id, RID = _load_ids()
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        sem = asyncio.Semaphore(8)
        async def login(i):
            async with sem:
                r = await client.post(f"{BASE}/api/auth/jwt/login", data={"username": f"load_u{i}@test.com", "password": "test1234"})
                return r.json()["access_token"]
        tokens = await asyncio.gather(*[login(i) for i in range(100)])

    latencies = []
    write_at = {'t': None}
    stop = asyncio.Event()

    async def watch(tok):
        async with httpx.AsyncClient(timeout=None, trust_env=False) as c:
            async with c.stream("GET", f"{BASE}/api/reports/activity/stream",
                                headers={"Authorization": f"Bearer {tok}", "X-Organization-Id": org_id}) as r:
                buf = ""
                async for chunk in r.aiter_text():
                    if stop.is_set(): return
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        dl = next((l for l in frame.split("\n") if l.startswith("data: ")), None)
                        if dl and write_at['t']:
                            ev = json.loads(dl[6:])
                            if ev.get('report_id') == RID and ev.get('state') == 'queued':
                                latencies.append(time.monotonic() - write_at['t'])
                                return

    tasks = [asyncio.create_task(watch(t)) for t in tokens]
    await asyncio.sleep(4)

    conn = await asyncpg.connect(DSN)
    now = datetime.datetime.utcnow()
    write_at['t'] = time.monotonic()
    await conn.execute(
        "INSERT INTO completions (id, prompt, completion, status, model, turn_index, message_type, role, report_id, main_router, feedback_score, created_at, updated_at) "
        "VALUES ($1, $2, $3, 'queued', 'mock-llm', 90, 'table', 'user', $4, 'chat', 0, $5, $6)",
        str(uuid.uuid4()), json.dumps({"content": "steady"}), json.dumps({}), RID, now, now)
    await conn.execute("UPDATE reports SET last_activity_at=$1 WHERE id=$2", now, RID)
    await conn.close()

    await asyncio.sleep(8)
    stop.set()
    for t in tasks: t.cancel()
    print(f"single-event delivery to {len(latencies)}/100 clients: p50={pct(latencies,0.5):.2f}s p95={pct(latencies,0.95):.2f}s max={max(latencies):.2f}s")

if __name__ == '__main__':
    asyncio.run(main())
